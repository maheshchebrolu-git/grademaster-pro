import random
import time
from playwright.sync_api import sync_playwright
from src.utils.notify import notify_and_wait

# --- DEVELOPER NOTES ---
# This script is the final 'hand-off'. It acts like me sitting
# at the keyboard, but it types at 100 words per minute and
# never makes a typo from the rubric results.


def _human_pause(min_s: float = 2.0, max_s: float = 5.0):
    """Random pause mimicking a human reading and thinking between students."""
    time.sleep(random.uniform(min_s, max_s))


def _typing_pause():
    """Short pause between filling fields, like a human tabbing through a form."""
    time.sleep(random.uniform(0.4, 1.2))


def _speedgrader_url(course_id: str, assignment_id: str, canvas_student_id: str) -> str:
    # Query param form is stable for direct deep-linking.
    return (
        f"https://canvas.gmu.edu/courses/{course_id}/gradebook/speed_grader"
        f"?assignment_id={assignment_id}&student_id={canvas_student_id}"
    )


def _speedgrader_hash_url(course_id: str, assignment_id: str, canvas_student_id: str) -> str:
    # Some Canvas deployments still honor student targeting via hash fragment.
    return (
        f"https://canvas.gmu.edu/courses/{course_id}/gradebook/speed_grader"
        f"?assignment_id={assignment_id}#{{'student_id':{canvas_student_id}}}"
    )


def _prompt_login_if_needed(page):
    login_markers = ("canvas.gmu.edu/login", "shibboleth.gmu.edu", "idp/profile")
    if any(marker in (page.url or "") for marker in login_markers):
        notify_and_wait(
            title="GradeMaster — Login Required",
            message="Canvas session expired during delivery. Complete SSO/Duo in browser then press ENTER.",
            prompt="🔐 Logged in? Press ENTER to continue: ",
        )


def _wait_for_visible_with_login(page, selector: str, timeout_ms: int):
    """
    Wait for a selector while handling mid-wait SSO redirects.
    """
    deadline = time.time() + (timeout_ms / 1000)
    while time.time() < deadline:
        _prompt_login_if_needed(page)
        try:
            page.locator(selector).first.wait_for(state="visible", timeout=1500)
            return page.locator(selector).first
        except Exception:
            time.sleep(0.5)
    raise TimeoutError(f"Timed out waiting for selector: {selector} (url={page.url})")


def _wait_for_visible_anywhere(page, selector: str, timeout_ms: int):
    """
    Wait for selector in main page or any iframe.
    Returns the first visible locator found.
    """
    deadline = time.time() + (timeout_ms / 1000)
    while time.time() < deadline:
        _prompt_login_if_needed(page)
        try:
            loc = page.locator(selector).first
            loc.wait_for(state="visible", timeout=400)
            return loc
        except Exception:
            pass

        for frame in page.frames:
            try:
                loc = frame.locator(selector).first
                loc.wait_for(state="visible", timeout=400)
                return loc
            except Exception:
                continue
        time.sleep(0.4)

    raise TimeoutError(f"Timed out waiting for selector anywhere: {selector} (url={page.url})")


def _open_student_speedgrader(page, course_id: str, assignment_id: str, canvas_student_id: str):
    """
    Navigate to a student's SpeedGrader page with fallback URL styles.
    """
    candidates = [
        _speedgrader_url(course_id, assignment_id, canvas_student_id),
        _speedgrader_hash_url(course_id, assignment_id, canvas_student_id),
    ]
    for idx, url in enumerate(candidates, start=1):
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(1200)
        _prompt_login_if_needed(page)
        if "/gradebook/speed_grader" in (page.url or ""):
            return
        if idx < len(candidates):
            print(f"⚠️ SpeedGrader deep link variant {idx} not ready, trying fallback URL...")


def _wait_until_ready(page):
    """
    Ensure we're on SpeedGrader and inputs are present.
    If SSO/MFA is required, wait for manual completion.
    """
    _prompt_login_if_needed(page)

    # Wait until grade field becomes visible/usable.
    try:
        _wait_for_visible_anywhere(
            page,
            "input[data-testid='grade-input'], #speedgrader_gist_grade, input[name='grading-box-extended'], input#grading-box-extended",
            timeout_ms=90000,
        )
    except Exception:
        print(f"❌ Could not find grade input on page URL: {page.url}")
        raise

    try:
        _wait_for_visible_anywhere(
            page,
            "#comment_submit_button, button[data-testid='submit-comment-button'], #speed_grader_comment_textarea, textarea[id*='comment'], textarea[name*='comment'], iframe.tox-edit-area__iframe, iframe[id^='rce-'][id$='_ifr']",
            timeout_ms=45000,
        )
    except Exception:
        print(f"❌ Could not find comment textarea on page URL: {page.url}")
        raise


def run_delivery_agent(course_id, assignment_id, section_data):
    """
    section_data would be the list of grades we pull from Supabase later.
    For now, I'm defining the 'actuator' logic.
    """
    with sync_playwright() as p:
        # Using the same session so I'm already logged into GMU Canvas
        context = p.chromium.launch_persistent_context(
            "playwright_session",
            headless=False
        )
        page = context.pages[0] if context.pages else context.new_page()
        # Open a real Canvas page up front so the browser is never blank.
        if section_data:
            first_student_id = str(section_data[0]["canvas_student_id"])
            page.goto(
                _speedgrader_url(str(course_id), str(assignment_id), first_student_id),
                wait_until="domcontentloaded",
            )
        else:
            page.goto("https://canvas.gmu.edu/login", wait_until="domcontentloaded")
        print("🔐 Complete Canvas login in the opened browser, then press ENTER to start delivery.")
        notify_and_wait(
            title="GradeMaster — Delivery Ready",
            message=f"Browser is open for delivery. Log in to Canvas then press ENTER to start typing grades.",
            prompt="🔐 Logged in? Press ENTER to start delivery: ",
        )

        for student in section_data:
            print(f"🤖 Navigating to {student['name']}...")

            _open_student_speedgrader(
                page,
                str(course_id),
                str(assignment_id),
                str(student["canvas_student_id"]),
            )
            _wait_until_ready(page)

            # Pause like a human landing on the page and reading the submission.
            _human_pause(3.0, 7.0)

            # 1. Type the Score
            score_input = _wait_for_visible_anywhere(
                page,
                "input[data-testid='grade-input'], #speedgrader_gist_grade, input[name='grading-box-extended'], input#grading-box-extended",
                timeout_ms=45000,
            )
            score_input.fill(str(student["final_score"]))

            # Pause between entering score and typing the comment.
            _typing_pause()

            # 2. Type the Feedback
            comment_text = str(student["ta_comment"])
            comment_filled = False

            # Legacy/plain textarea fallback.
            if comment_text.strip():
                try:
                    comment_box = _wait_for_visible_anywhere(
                        page,
                        "#speed_grader_comment_textarea, textarea[id*='comment'], textarea[name*='comment']",
                        timeout_ms=2000,
                    )
                    comment_box.fill(comment_text)
                    comment_filled = True
                except Exception:
                    pass

            # Canvas RCE (TinyMCE iframe) path from provided DOM.
            if comment_text.strip() and not comment_filled:
                iframe_selectors = ["iframe.tox-edit-area__iframe", "iframe[id^='rce-'][id$='_ifr']"]
                for iframe_sel in iframe_selectors:
                    try:
                        _wait_for_visible_anywhere(page, iframe_sel, timeout_ms=10000)
                        body = page.frame_locator(iframe_sel).locator(
                            "body#tinymce, body.mce-content-body, body"
                        ).first
                        body.wait_for(state="visible", timeout=10000)
                        body.click()
                        body.fill(comment_text)
                        comment_filled = True
                        break
                    except Exception:
                        continue

            if comment_text.strip() and not comment_filled:
                raise TimeoutError(f"Could not fill comment editor on page URL: {page.url}")

            # Submit comment (only if there is one to submit).
            if comment_text.strip():
                submit_btn = _wait_for_visible_anywhere(
                    page,
                    "#comment_submit_button, button[data-testid='submit-comment-button'], button:has-text('Submit'), button[type='submit']",
                    timeout_ms=30000,
                )
                submit_btn.click()

            print(f"✅ {student['name']} — score: {student['final_score']} | comment: {student['ta_comment'] or '(none)'}")

            # Pause between students — longer, like a TA moving on after reviewing.
            _human_pause(4.0, 9.0)
