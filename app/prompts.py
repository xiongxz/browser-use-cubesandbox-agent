from __future__ import annotations

from .feishu_form_fill import PRESET_FEISHU_FORM_FIELD_IDS, display_time_to_timestamp_ms
from .models import BrowserAgentRunRequest


FEISHU_DEFAULT_DOMAINS = [
    "*.feishu.cn",
    "*.larksuite.com",
    "*.larkoffice.com",
]


def effective_allowed_domains(request: BrowserAgentRunRequest) -> list[str]:
    if request.allowed_domains:
        return request.allowed_domains
    if request.mode in {"feishu_bitable_to_form", "feishu_form_fill"}:
        return FEISHU_DEFAULT_DOMAINS.copy()
    return []


def _feishu_field_ids_text(request: BrowserAgentRunRequest) -> str:
    field_ids = {**PRESET_FEISHU_FORM_FIELD_IDS, **request.feishu_field_ids}
    return (
        f"姓名 field id {field_ids['name']} uses {{type:1,value:[{{type:'text',text:name}}]}}; "
        f"参会时间 field id {field_ids['attendance_time']} uses {{type:5,value:timestamp_ms}}; "
        f"人数/参会人数 field id {field_ids['attendance_count']} uses {{type:2,value:number}}."
    )


def build_task_prompt(request: BrowserAgentRunRequest) -> str:
    if request.mode == "feishu_bitable_to_form":
        assert request.bitable_url is not None
        if request.require_human_confirmation and not request.human_confirmation_granted:
            return (
                "You are operating Feishu to convert a Bitable into a questionnaire.\n"
                "Current phase: draft review before sharing.\n"
                "Goal:\n"
                f"- Open this Feishu Bitable URL: {request.bitable_url}\n"
                "- Stay focused on the current workspace and do not edit unrelated content.\n"
                "- Look for the 'Generate Form/生成表单' or 'Convert to Form/转问卷' button (usually in the top-right corner).\n"
                "- Click the button to CREATE a new form/questionnaire view for this Bitable.\n"
                "- Once inside the form/questionnaire editing interface, capture the visible questionnaire title and draft questions.\n"
                "- Do NOT click share, do NOT enable form sharing, and do NOT finish by returning a link in this phase.\n"
                "- Stop only after you can present the draft questionnaire for human review.\n"
                "- IMPORTANT: Leave the browser on the form editing page when done (this helps the next phase locate the same view).\n"
                "- If the page requires login or permission that is unavailable, stop and explain the exact blocker.\n"
                "- Return awaiting_human_confirmation=true together with the draft questions and form name.\n"
                "- Common labels may include Chinese or English variants such as: "
                "'生成表单', '转问卷', '转换为问卷', '新建表单', 'Create Form', 'Generate Form'.\n"
                "- The source user request is below.\n\n"
                f"User request: {request.query}"
            )

        confirmation_notes = request.human_confirmation_notes.strip() if request.human_confirmation_notes else ""
        return (
            "You are operating Feishu to convert a Bitable into a questionnaire.\n"
            "Current phase: publish/share after human confirmation.\n"
            "Goal:\n"
            f"- Open this Feishu Bitable URL: {request.bitable_url}\n"
            "- Stay focused on the current workspace and do not edit unrelated content.\n"
            "- FIRST, check if a form/questionnaire view already exists for this Bitable:\n"
            "  * Look for 'Form/表单' or 'Questionnaire/问卷' in the view tabs or sidebar.\n"
            "  * If you find an existing form view, click to enter the editing interface.\n"
            "  * If NO form view exists, click 'Generate Form/生成表单' or 'Convert to Form/转问卷' (usually top-right) to CREATE one first.\n"
            "- Once inside the form/questionnaire editing interface:\n"
            "  * If there are human review notes, apply the requested edits carefully before sharing.\n"
            "  * CRITICAL: Look for a button labeled '分享表单' (Share Form) - NOT just '分享' (Share).\n"
            "  * There are TWO buttons with similar names: '分享' and '分享表单'. You MUST click '分享表单'.\n"
            "  * If the button says '已开启分享' (Sharing Enabled) instead of '分享表单', click '已开启分享' - this means sharing is already on.\n"
            "  * After clicking '分享表单' or '已开启分享', a dialog will appear.\n"
            "  * In the dialog, if you see an '开启表单分享' (Enable form sharing) switch that is OFF, turn it ON.\n"
            "  * Wait about 2 seconds after enabling the switch for the questionnaire link to appear.\n"
            "  * Capture the final shareable questionnaire link from the dialog (it should be a full URL starting with https://).\n"
            "- Your finish condition is strict: do not stop until you have the real final questionnaire link visible and captured.\n"
            "- A successful run must end with the actual questionnaire URL in form_url.\n"
            "- Common labels: '转问卷', '生成表单', '分享表单' (correct button), '已开启分享', '开启表单分享'.\n"
            "- If the page requires login or permission that is unavailable, stop and explain the exact blocker.\n"
            "- Do not fabricate a URL. If you cannot see the final questionnaire link, report failure.\n"
            f"- Human review notes to apply before sharing: {confirmation_notes or 'No extra edits requested; proceed with the confirmed draft.'}\n"
            "- The source user request is below.\n\n"
            f"User request: {request.query}"
        )

    if request.mode == "feishu_form_fill":
        assert request.form_url is not None
        if request.require_human_confirmation and not request.human_confirmation_granted:
            return (
                "You are operating a prebuilt Feishu questionnaire/form.\n"
                "Current phase: draft answer preparation before final submission.\n"
                "Goal:\n"
                f"- Open this Feishu form URL: {request.form_url}\n"
                "- Inspect the visible form title and each visible question/field in order.\n"
                "- Parse the user's natural-language request and infer the best answer for each field.\n"
                "- Fill the visible form fields if that helps you verify the mapping, but DO NOT click any final submit button such as '提交', 'Submit', '确认', or similar.\n"
                "- Return awaiting_human_confirmation=true together with the captured form name and draft_answers.\n"
                "- For every visible field, return one draft_answers item including: index, field_label, question_type, required, proposed_value, normalized_values, confidence, and source_excerpt when possible.\n"
                "- If a field cannot be safely inferred, leave proposed_value empty/null, keep normalized_values empty, and mention the blocker in notes.\n"
                "- Do not fabricate missing personal details.\n"
                "- Stop only after you can present a complete reviewable draft answer set for human confirmation.\n"
                "- If the page requires login or permission that is unavailable, stop and explain the exact blocker.\n"
                "- The source user request is below.\n\n"
                f"User request: {request.query}"
            )

        confirmation_notes = request.human_confirmation_notes.strip() if request.human_confirmation_notes else ""
        confirmed_blocks: list[str] = []
        for answer in request.confirmed_answers:
            target = f"{answer.index}. {answer.field_label}" if answer.index is not None and answer.field_label else (
                str(answer.index) if answer.index is not None else (answer.field_label or "unknown field")
            )
            if answer.field_key == "attendance_time" and answer.confirmed_value:
                timestamp_ms = display_time_to_timestamp_ms(answer.confirmed_value)
                final_value = f"display={answer.confirmed_value}; timestamp_ms={timestamp_ms}"
            else:
                values = answer.normalized_values or ([answer.confirmed_value] if answer.confirmed_value else [])
                final_value = " / ".join(values) if values else ("<clear this field>" if answer.clear_value else "<keep drafted value>")
            confirmed_blocks.append(f"- {target}: {final_value}")

        confirmed_answers_text = "\n".join(confirmed_blocks) if confirmed_blocks else "- No explicit overrides; reuse the drafted answers from phase 1."
        explicit_name = next((a.confirmed_value for a in request.confirmed_answers if a.field_key == "name" and a.confirmed_value), None)
        explicit_time = next((a.confirmed_value for a in request.confirmed_answers if a.field_key == "attendance_time" and a.confirmed_value), None)
        explicit_count = next((a.confirmed_value for a in request.confirmed_answers if a.field_key == "attendance_count" and a.confirmed_value), None)
        return (
            "You are operating a prebuilt Feishu questionnaire/form.\n"
            "Current phase: final fill and submit after human confirmation.\n"
            "Goal:\n"
            f"- Open this Feishu form URL: {request.form_url}\n"
            "- Before interacting with fields or clicking submit, ensure the custom action `install_feishu_form_submit_payload_guard` has been run. This guards the final Feishu request payload with the confirmed field values.\n"
            "- The form fields are fixed and must be filled in this schema:\n"
            "  * 姓名 -> string\n"
            "  * 参会时间 -> timestamp in milliseconds (if the UI shows a date picker, use the display value but keep it semantically equal to the timestamp)\n"
            "  * 人数 / 参会人数 -> number\n"
            f"- The expected Feishu wire payload shape for this form is: {_feishu_field_ids_text(request)}\n"
            "- Fill the form according to the confirmed answers below.\n"
            "- IMPORTANT FIELD-BY-FIELD EXECUTION RULES:\n"
            "  * First find the field labeled exactly '姓名'. Treat it as a plain text/string field, not a people picker. Fill it with the expected name value shown below.\n"
            "  * For 姓名, try these text-field strategies until the name is visibly committed: click the input directly under/right of the label and type; if it stays blank, click the surrounding field card/container and type; if needed, clear the active field with Ctrl+A/Backspace and type again; press Tab, Enter, or click outside to commit. The control may be an input, textarea, or contenteditable-style custom Feishu text field.\n"
            "  * If a suggestion dropdown appears while filling 姓名, do not treat it as required unless the text field refuses free text. Prefer leaving the literal confirmed name in the text field over selecting an unrelated person/entity.\n"
            "  * Visually confirm the exact 姓名 value is still visible inside the field after blur/focus changes before moving on.\n"
            "  * Then find the field labeled exactly '参会时间'. If the UI is a date picker, select/type the human-readable date that corresponds to the confirmed timestamp. Visually confirm the chosen date is visible.\n"
            "  * Then find the numeric field labeled '人数' or '参会人数'. Click the numeric input, type the confirmed number, and visually confirm the number remains visible.\n"
            "  * Do NOT rely only on field order if labels are visible; labels take priority.\n"
            "  * If any field value is not visibly present after typing, retry that field instead of submitting.\n"
            "- FINAL PRE-SUBMIT CHECK IS MANDATORY:\n"
            "  * Before clicking submit, verify all three fields are visibly populated on screen: 姓名 is non-empty text, 参会时间 shows the expected date, and 人数/参会人数 shows the expected number.\n"
            "  * If you cannot visually verify the 姓名 field is populated, do NOT submit. Report failure instead.\n"
            f"- Expected concrete values for this run: 姓名={explicit_name or '(missing)'}; 参会时间={explicit_time or '(missing)'}; 参会人数={explicit_count or '(missing)'}.\n"
            "- After all required fields are correctly filled, click the final submit button.\n"
            "- Wait for the confirmation screen, toast, or success message, and capture the visible submission result.\n"
            "- If no visible success message appears after clicking submit, call `get_feishu_form_submit_payload_guard_status` and inspect guarded submission attempts.\n"
            "- DO NOT call done after clicking submit unless either: (1) visible success text is present, OR (2) `get_feishu_form_submit_payload_guard_status` has been called and checked.\n"
            "- If the submit button click appears uncertain or the page stays on the form, click submit again if the filled values are still visible, then call `get_feishu_form_submit_payload_guard_status` before deciding.\n"
            "- A successful run must end only after the form is truly submitted.\n"
            "- If the page becomes Submitted / 提交成功 after you click the final submit button, mark success=true and set submission_result to the visible success text.\n"
            "- If `get_feishu_form_submit_payload_guard_status` shows a guarded submission attempt with ok=true, HTTP 2xx, code=0, or success=true, mark success=true and set submission_result='Submitted (network verified)'.\n"
            "- If the form is already submitted before you can fill and submit the confirmed values, blocked by permissions, or contains validation errors, explain the exact blocker in notes and mark success=false.\n"
            f"- Human confirmation notes: {confirmation_notes or 'No extra notes. If the user supplied new data after review, that should have been handled by rerunning the prepare phase instead of this submit phase.'}\n"
            "- Confirmed answers to apply:\n"
            f"{confirmed_answers_text}\n"
            "- Confirmed answers are authoritative in this phase.\n"
        )

    prompt_lines = [
        "Use the browser to complete the user's request safely and efficiently.",
        "Prefer deterministic navigation and stop as soon as you have the final result.",
        f"User request: {request.query}",
    ]
    if request.start_url:
        prompt_lines.append(f"Start from this URL first: {request.start_url}")
    if request.allowed_domains:
        prompt_lines.append(f"Stay within these allowed domains if possible: {', '.join(request.allowed_domains)}")
    return "\n".join(prompt_lines)
