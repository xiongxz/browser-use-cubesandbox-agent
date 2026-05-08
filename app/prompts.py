from __future__ import annotations

from .models import BrowserAgentRunRequest


FEISHU_DEFAULT_DOMAINS = [
    "*.feishu.cn",
    "*.larksuite.com",
    "*.larkoffice.com",
]


def effective_allowed_domains(request: BrowserAgentRunRequest) -> list[str]:
    if request.allowed_domains:
        return request.allowed_domains
    if request.mode == "feishu_bitable_to_form":
        return FEISHU_DEFAULT_DOMAINS.copy()
    return []


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
