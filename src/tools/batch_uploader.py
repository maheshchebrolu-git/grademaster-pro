import os
import json
import mimetypes
import re
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from google.cloud import storage
from google import genai
from dotenv import load_dotenv

load_dotenv()

GCP_BUCKET = os.getenv("GCP_BUCKET_NAME")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
SOLUTION_PDF_LOCAL_PATH = os.path.expanduser(
    os.getenv("SOLUTION_PDF_LOCAL_PATH", "~/documents/grading/assets/solutions.pdf")
)


def _local_batch_file(assignment, section):
    return os.path.expanduser(
        f"~/documents/grading/{assignment}/batch_files/batch_input_{section}.jsonl"
    )


def _session_meta_path(assignment, section):
    return os.path.expanduser(
        f"~/documents/grading/{assignment}/batch_files/session_{section}.json"
    )


def _prepared_batch_file(assignment, section):
    return os.path.expanduser(
        f"~/documents/grading/{assignment}/batch_files/batch_input_{section}_prepared.jsonl"
    )


def _genai_client():
    if not GEMINI_API_KEY:
        raise ValueError("❌ GEMINI_API_KEY is missing in .env")
    return genai.Client(api_key=GEMINI_API_KEY)


def _resolve_solution_pdf_path(assignment: str) -> str:
    """
    Resolve active solution PDF path for an assignment.
    Priority:
    1) SOLUTION_PDF_LOCAL_PATH from env (setup-managed)
    2) ~/documents/grading/<assignment>/assets/solutions.pdf
    3) ~/documents/grading/<assignment>/prompts/solutions.pdf
    """
    candidates = [
        os.path.expanduser(os.getenv("SOLUTION_PDF_LOCAL_PATH", "")),
        os.path.expanduser(f"~/documents/grading/{assignment}/assets/solutions.pdf"),
        os.path.expanduser(f"~/documents/grading/{assignment}/prompts/solutions.pdf"),
    ]
    for path in candidates:
        if path and os.path.exists(path):
            return path
    return candidates[0] if candidates else ""


@dataclass
class UploadArtifact:
    path: str
    mime_type: str
    kind: Literal["original", "docx_text", "docx_image", "docx_pdf"]
    source_file: str


@dataclass
class DocxExtractionResult:
    text_artifact: UploadArtifact | None
    image_artifacts: list[UploadArtifact]
    warnings: list[str]


def _mime_for_file(file_path: str) -> str:
    ext = os.path.splitext(file_path.lower())[1]
    if ext == ".pdf":
        return "application/pdf"
    if ext == ".png":
        return "image/png"
    if ext in {".jpg", ".jpeg"}:
        return "image/jpeg"
    guessed, _ = mimetypes.guess_type(file_path)
    return guessed or "application/octet-stream"


def _extract_docx_text(docx_path: str, temp_dir: str) -> UploadArtifact | None:
    """
    Parse DOCX text and write to a temporary .txt artifact.
    """
    with zipfile.ZipFile(docx_path, "r") as zf:
        xml = zf.read("word/document.xml").decode("utf-8", errors="ignore")
    text = re.sub(r"</w:p>", "\n", xml)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        return None

    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".txt",
        delete=False,
        encoding="utf-8",
        dir=temp_dir,
    ) as tf:
        tf.write(text)
        return UploadArtifact(
            path=tf.name,
            mime_type="text/plain",
            kind="docx_text",
            source_file=docx_path,
        )


def _extract_docx_images(docx_path: str, temp_dir: str) -> tuple[list[UploadArtifact], list[str]]:
    """
    Extract embedded images from word/media/* and return upload artifacts.
    """
    artifacts: list[UploadArtifact] = []
    warnings: list[str] = []
    with zipfile.ZipFile(docx_path, "r") as zf:
        media_files = [n for n in zf.namelist() if n.startswith("word/media/") and not n.endswith("/")]
        for idx, member in enumerate(media_files, start=1):
            ext = Path(member).suffix.lower()
            if ext not in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
                warnings.append(f"Unsupported embedded image type in {docx_path}: {member}")
                continue
            mime = _mime_for_file(member)
            out_name = f"{Path(docx_path).stem}_img_{idx}{ext}"
            out_path = os.path.join(temp_dir, out_name)
            with open(out_path, "wb") as f:
                f.write(zf.read(member))
            artifacts.append(
                UploadArtifact(
                    path=out_path,
                    mime_type=mime,
                    kind="docx_image",
                    source_file=docx_path,
                )
            )
    return artifacts, warnings


def _extract_docx_artifacts(docx_path: str, temp_dir: str) -> DocxExtractionResult:
    text_artifact = _extract_docx_text(docx_path, temp_dir)
    image_artifacts, warnings = _extract_docx_images(docx_path, temp_dir)
    return DocxExtractionResult(
        text_artifact=text_artifact,
        image_artifacts=image_artifacts,
        warnings=warnings,
    )


def _find_libreoffice_executable() -> str | None:
    """Resolve `soffice` / `libreoffice` for headless --convert-to pdf."""
    for name in ("soffice", "libreoffice"):
        found = shutil.which(name)
        if found:
            return found
    mac = "/Applications/LibreOffice.app/Contents/MacOS/soffice"
    if os.path.isfile(mac) and os.access(mac, os.X_OK):
        return mac
    return None


def _convert_docx_to_pdf_libreoffice(docx_path: str, out_dir: str) -> tuple[str | None, list[str]]:
    """
    Use LibreOffice headless to render DOCX as PDF (preserves layout and inline images).
    Returns (path_to_pdf, warnings) or (None, warnings) on failure.
    """
    warnings: list[str] = []
    exe = _find_libreoffice_executable()
    if not exe:
        warnings.append(
            "LibreOffice not found (install from libreoffice.org or `brew install --cask libreoffice`). "
            "Falling back to text+image extraction."
        )
        return None, warnings

    docx_abs = os.path.abspath(docx_path)
    out_abs = os.path.abspath(out_dir)
    os.makedirs(out_abs, exist_ok=True)

    timeout_sec = int(os.getenv("GRADEMASTER_LIBREOFFICE_TIMEOUT_SEC", "180"))
    try:
        proc = subprocess.run(
            [exe, "--headless", "--convert-to", "pdf", "--outdir", out_abs, docx_abs],
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
    except subprocess.TimeoutExpired:
        warnings.append(f"LibreOffice timed out after {timeout_sec}s for {docx_path}")
        return None, warnings

    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()
        tail = tail[:800] if tail else "(no output)"
        warnings.append(f"LibreOffice convert failed (exit {proc.returncode}): {tail}")
        return None, warnings

    pdf_name = Path(docx_abs).stem + ".pdf"
    pdf_path = os.path.join(out_abs, pdf_name)
    if not os.path.isfile(pdf_path) or os.path.getsize(pdf_path) == 0:
        warnings.append(f"LibreOffice finished but PDF missing or empty: {pdf_path}")
        return None, warnings

    return pdf_path, warnings


def _docx_use_libreoffice_first() -> bool:
    v = (os.getenv("GRADEMASTER_DOCX_CONVERSION", "libreoffice") or "").strip().lower()
    return v in ("", "libreoffice", "lo", "1", "true", "yes")


def _prepare_upload_artifacts(file_path: str, temp_dir: str) -> tuple[list[UploadArtifact], list[str]]:
    """
    Expand one source file into one-or-more upload artifacts.
    """
    ext = Path(file_path).suffix.lower()
    if ext == ".docx":
        all_warnings: list[str] = []

        if _docx_use_libreoffice_first():
            pdf_path, lo_warnings = _convert_docx_to_pdf_libreoffice(file_path, temp_dir)
            all_warnings.extend(lo_warnings)
            if pdf_path and os.path.isfile(pdf_path) and os.path.getsize(pdf_path) > 0:
                return (
                    [
                        UploadArtifact(
                            path=pdf_path,
                            mime_type="application/pdf",
                            kind="docx_pdf",
                            source_file=file_path,
                        )
                    ],
                    all_warnings,
                )

        docx_data = _extract_docx_artifacts(file_path, temp_dir)
        if _docx_use_libreoffice_first() and _find_libreoffice_executable():
            all_warnings.append(
                f"Using legacy DOCX extraction (text + embedded images) for {Path(file_path).name}."
            )

        artifacts: list[UploadArtifact] = []
        if docx_data.text_artifact:
            artifacts.append(docx_data.text_artifact)
        artifacts.extend(docx_data.image_artifacts)
        if not artifacts:
            return [], all_warnings + [f"No usable text/images extracted from DOCX: {file_path}"]
        return artifacts, all_warnings + docx_data.warnings

    mime = _mime_for_file(file_path)
    if mime == "application/octet-stream":
        return [], [f"Unsupported/unknown mime for file: {file_path}"]
    return [UploadArtifact(path=file_path, mime_type=mime, kind="original", source_file=file_path)], []


def _upload_artifacts_and_build_parts(client, artifacts: list[UploadArtifact], tag: str = "") -> tuple[list[dict], list[str]]:
    parts: list[dict] = []
    warnings: list[str] = []
    for artifact in artifacts:
        try:
            upload_config = {"mime_type": artifact.mime_type}
            if tag:
                # Tag every uploaded file with assignment_section so cleanup can find them.
                upload_config["display_name"] = f"{tag}_{Path(artifact.path).name}"
            uploaded = client.files.upload(file=artifact.path, config=upload_config)
            uri = getattr(uploaded, "uri", None)
            if not uri:
                warnings.append(f"Upload returned no uri for {artifact.path}")
                continue
            parts.append(
                {
                    "file_data": {
                        "mime_type": artifact.mime_type,
                        "file_uri": uri,
                    }
                }
            )
        except Exception as exc:
            warnings.append(f"Upload failed for {artifact.path}: {exc}")
    return parts, warnings


def _prepare_student_files_batch(local_file: str, prepared_file: str, client, solution_file_ref, tag: str = "") -> dict:
    """
    Read harvested JSONL, upload each student's local files, and inject file URIs
    into the per-student request content for batch processing.
    """
    transformed = 0
    skipped = 0
    failed_students = []

    solution_uri = None
    solution_mime = None
    if isinstance(solution_file_ref, dict):
        solution_uri = solution_file_ref.get("uri")
        solution_mime = solution_file_ref.get("mime_type", "application/pdf")

    temp_paths_to_cleanup: list[str] = []
    with open(local_file, "r", encoding="utf-8") as src, open(prepared_file, "w", encoding="utf-8") as out:
        for raw_line in src:
            line = raw_line.strip()
            if not line:
                continue
            student = json.loads(line)
            student_files = student.get("local_file_paths", [])

            student_parts: list[dict] = []
            student_warnings: list[str] = []
            for file_path in student_files:
                if not os.path.exists(file_path):
                    print(f"⚠️ Missing local submission file: {file_path}")
                    continue
                with tempfile.TemporaryDirectory(prefix="gm_docx_") as temp_dir:
                    artifacts, warnings = _prepare_upload_artifacts(file_path, temp_dir)
                    student_warnings.extend(warnings)
                    # Persist temp artifact copies while uploading to avoid
                    # race with context manager cleanup.
                    persistent_artifacts: list[UploadArtifact] = []
                    for artifact in artifacts:
                        if artifact.path.startswith(temp_dir):
                            keep_path = tempfile.NamedTemporaryFile(delete=False, suffix=Path(artifact.path).suffix).name
                            with open(artifact.path, "rb") as srcf, open(keep_path, "wb") as dstf:
                                dstf.write(srcf.read())
                            temp_paths_to_cleanup.append(keep_path)
                            persistent_artifacts.append(
                                UploadArtifact(
                                    path=keep_path,
                                    mime_type=artifact.mime_type,
                                    kind=artifact.kind,
                                    source_file=artifact.source_file,
                                )
                            )
                        else:
                            persistent_artifacts.append(artifact)
                    parts, upload_warnings = _upload_artifacts_and_build_parts(client, persistent_artifacts, tag=tag)
                    student_parts.extend(parts)
                    student_warnings.extend(upload_warnings)

            for warn in student_warnings:
                print(f"⚠️ {warn}")

            # Build schema-compliant batch record:
            # Only `key` + `request` should be sent to Gemini batch API.
            key = student.get("key") or student.get("custom_id") or "unknown_student"
            request = student.get("request")

            # Backward-compat for older harvester JSONL format.
            if request is None:
                messages = student.get("body", {}).get("messages", [])
                content_parts = []
                if messages and isinstance(messages[0].get("content"), list):
                    for part in messages[0]["content"]:
                        if isinstance(part, dict) and "text" in part:
                            content_parts.append({"text": part["text"]})
                request = {
                    "contents": [
                        {
                            "role": "user",
                            "parts": content_parts or [{"text": "Please grade this submission."}],
                        }
                    ]
                }

            contents = request.get("contents", [])
            if not contents:
                contents = [{"role": "user", "parts": [{"text": "Please grade this submission."}]}]
                request["contents"] = contents

            first_content = contents[0]
            parts = first_content.get("parts", [])
            if not isinstance(parts, list):
                parts = [{"text": "Please grade this submission."}]

            if solution_uri:
                parts.insert(
                    0,
                    {
                        "file_data": {
                            "mime_type": solution_mime,
                            "file_uri": solution_uri,
                        }
                    },
                )

            if not student_parts:
                skipped += 1
                failed_students.append(key)
                print(f"⚠️ Skipping {key}: no usable uploaded submission files.")
                continue

            # Inject all uploaded submission artifacts.
            parts.extend(student_parts)
            first_content["parts"] = parts
            contents[0] = first_content
            request["contents"] = contents

            clean_record = {
                "key": key,
                "request": request,
            }

            out.write(json.dumps(clean_record) + "\n")
            transformed += 1

    for temp_path in temp_paths_to_cleanup:
        if os.path.exists(temp_path):
            os.remove(temp_path)

    return {
        "transformed": transformed,
        "skipped": skipped,
        "failed_students": failed_students,
    }


def _validate_prepared_batch_file(prepared_file: str) -> tuple[bool, str]:
    allowed_keys = {"key", "request"}
    line_no = 0
    with open(prepared_file, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line:
                continue
            line_no += 1
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                return False, f"Line {line_no}: invalid JSON ({exc})"
            if not isinstance(obj, dict):
                return False, f"Line {line_no}: record is not an object."
            unknown = set(obj.keys()) - allowed_keys
            if unknown:
                return False, f"Line {line_no}: unknown keys found: {sorted(unknown)}"
            if "key" not in obj or "request" not in obj:
                return False, f"Line {line_no}: missing key/request."
    if line_no == 0:
        return False, "Prepared batch is empty."
    return True, "ok"


def _purge_previous_upload_files(client, assignment: str, section: str):
    """
    Delete all AI Studio files from the previous upload run for this
    assignment+section, using the session metadata file as the source of truth.
    Falls back to name-tag scanning if metadata is missing.
    """
    deleted = 0

    # Primary: delete exactly the files recorded in the last session metadata.
    meta_path = _session_meta_path(assignment, section)
    if os.path.exists(meta_path):
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except Exception:
            meta = {}

        # Collect every file name recorded in that session.
        names_to_delete: list[str] = []

        batch_file_name = meta.get("batch_input_file_name")
        if batch_file_name:
            names_to_delete.append(batch_file_name)

        sol_ref = meta.get("registered_solution_file")
        if isinstance(sol_ref, dict) and sol_ref.get("name"):
            names_to_delete.append(sol_ref["name"])

        for name in names_to_delete:
            try:
                client.files.delete(name=name)
                deleted += 1
            except Exception:
                pass  # already deleted or never existed

    # Secondary: scan all files and delete any tagged with this assignment+section
    # (catches student submission files that aren't individually tracked in metadata).
    tag = f"{assignment}_{section}".lower()
    try:
        for file_obj in client.files.list():
            display = (getattr(file_obj, "display_name", "") or "").lower()
            name    = (getattr(file_obj, "name", "") or "").lower()
            if tag in display or tag in name:
                try:
                    client.files.delete(name=file_obj.name)
                    deleted += 1
                except Exception:
                    pass
    except Exception:
        pass

    if deleted:
        print(f"🗑️  Purged {deleted} stale AI Studio files from previous run.")


def register_solution_file(assignment):
    """
    Register a persistent solution PDF with Google AI Studio File API.
    Returns file metadata to reuse across the grading session.
    """
    solution_path = _resolve_solution_pdf_path(assignment)
    if not solution_path or not os.path.exists(solution_path):
        print(f"⚠️ Solution PDF missing at: {solution_path or SOLUTION_PDF_LOCAL_PATH}. Skipping registration.")
        return None

    print("🔐 Registering solution PDF with Google AI Studio...")
    client = _genai_client()
    registered = client.files.upload(
        file=solution_path,
        config={"mime_type": "application/pdf"},
    )
    uri = getattr(registered, "uri", "")
    name = getattr(registered, "name", None)
    print(f"✅ Solution PDF registered. URI: {uri}")
    return {
        "name": name,
        "uri": uri,
        "mime_type": "application/pdf",
    }


def upload_and_trigger(assignment, section):
    local_file = _local_batch_file(assignment, section)
    prepared_file = _prepared_batch_file(assignment, section)

    if not os.path.exists(local_file):
        print(f"❌ File not found at {local_file}")
        return None

    client = _genai_client()

    # Purge stale files from the previous run before uploading fresh ones.
    print(f"🧹 Cleaning up previous AI Studio files for {assignment}/{section}...")
    _purge_previous_upload_files(client, assignment, section)

    solution_file_ref = register_solution_file(assignment)
    tag = f"{assignment}_{section}"
    prep_stats = _prepare_student_files_batch(local_file, prepared_file, client, solution_file_ref, tag=tag)
    print(
        "🧠 Prepared "
        f"{prep_stats['transformed']} payloads; skipped {prep_stats['skipped']} with no usable files."
    )

    is_valid, validation_msg = _validate_prepared_batch_file(prepared_file)
    if not is_valid:
        print(f"❌ Prepared batch validation failed: {validation_msg}")
        return {
            "assignment": assignment,
            "section": section,
            "status": "prepared_validation_failed",
            "validation_error": validation_msg,
            "failed_students": prep_stats["failed_students"],
        }

    # Upload transformed batch JSONL to AI Studio File API.
    print("📄 Uploading prepared batch JSONL to Google AI Studio File API...")
    batch_input_file = client.files.upload(
        file=prepared_file,
        config={"mime_type": "text/plain"},
    )
    batch_input_file_name = getattr(batch_input_file, "name", None)
    batch_input_file_uri = getattr(batch_input_file, "uri", None)

    gcs_uri = None
    if GCP_BUCKET:
        # Optional: keep a copy in your GCS bucket for your own storage/history.
        print("📦 Uploading backup copy to Google Cloud Storage...")
        storage_client = storage.Client()
        bucket = storage_client.bucket(GCP_BUCKET)
        blob_path = f"batches/{assignment}/{section}_input.jsonl"
        blob = bucket.blob(blob_path)
        blob.upload_from_filename(prepared_file)
        gcs_uri = f"gs://{GCP_BUCKET}/{blob_path}"

    # 2. Trigger AI Studio batch job (if available in SDK/runtime)
    batch_job_id = None
    print(f"🚀 Starting Batch Job for {assignment} {section}...")
    try:
        batch_job = client.batches.create(
            model="models/gemini-2.5-pro",
            src=batch_input_file_name,
            config={"display_name": f"{assignment}_{section}_grading"},
        )
        batch_job_id = getattr(batch_job, "name", None)
    except Exception as exc:
        print(f"⚠️ Batch create not executed in this environment: {exc}")
        print("⚠️ Input file is uploaded and ready for manual submission.")

    # Save a session metadata file for later sync/cleanup.
    session_meta = {
        "assignment": assignment,
        "section": section,
        "batch_job_id": batch_job_id,
        "batch_input_gcs_uri": gcs_uri,
        "prepared_batch_path": prepared_file,
        "batch_input_file_name": batch_input_file_name,
        "batch_input_file_uri": batch_input_file_uri,
        "registered_solution_file": solution_file_ref,
        "status": "batch_created" if batch_job_id else "uploaded_only",
        "failed_students": prep_stats["failed_students"],
    }
    with open(_session_meta_path(assignment, section), "w", encoding="utf-8") as f:
        json.dump(session_meta, f, indent=2)

    if batch_job_id:
        print("✅ Job live! Session metadata saved for sync/cleanup.")
    else:
        print("⚠️ No batch job ID returned. Session metadata saved as uploaded_only.")
    return session_meta


def cloud_ephemeral_flow(assignment, section):
    result = upload_and_trigger(assignment.lower(), section.lower())
    if isinstance(result, dict) and result.get("status") in {"prepared_validation_failed"}:
        raise RuntimeError(f"Upload failed: {result.get('validation_error')}")
    return result


def load_session_meta(assignment, section):
    path = _session_meta_path(assignment.lower(), section.lower())
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def unregister_solution_file(assignment, section):
    session_meta = load_session_meta(assignment, section)
    if not session_meta:
        print("⚠️ No session metadata found. Skipping unregister.")
        return

    file_ref = session_meta.get("registered_solution_file")
    if not file_ref:
        print("⚠️ No registered solution file found in session metadata.")
        return

    print("🧹 Unregistering solution PDF from Google AI Studio File API...")
    client = _genai_client()
    name = file_ref.get("name") if isinstance(file_ref, dict) else file_ref
    client.files.delete(name=name)
    print("✅ Solution PDF unregistered.")


if __name__ == "__main__":
    cloud_ephemeral_flow("lab2", "dl2")