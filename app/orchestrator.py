from __future__ import annotations

import base64
import json
import uuid
from typing import Any

import httpx

from .config import MEDIA_DIR
from .db import connect, utcnow
from .cliproxy import CLIProxyError, automatic_route, chat_completions, generate_image, model_family
from .integrations import (
    asterisk_originate,
    calendar_list,
    emby_request,
    gmail_list,
    homeassistant_request,
    instagram_media,
    instagram_profile,
    spotify_current,
    tasks_list,
    youtube_channel,
)
from .creative import add_prompt, create_project, list_projects, prompts_that_worked, update_project_status
from .mcp_client import call_cached_tool, cached_tools_as_functions, tool_requires_confirmation as mcp_tool_requires_confirmation
from .memory import add_memory, search_memory
from .project_files import list_files as list_project_files, mounted as project_files_mounted, read_text_file as read_project_file
from .permissions import is_allowed
from .routines import create_routine as create_routine_row, list_routines
from .scheduler import schedule_routine
from .security import audit

# Short, opinionated cheat-sheets so craft_prompt has real vocabulary to work
# with instead of inventing plausible-sounding but generic prompt language.
# These are starting points, not gospel — Tommy's own "worked" prompts
# (prompts_that_worked) always take priority over this glossary.
PLATFORM_NOTES = {
    "suno": (
        "Suno prompts read like a style brief, not a sentence: genre + mood + era + instrumentation + "
        "vocal style + tempo/BPM feel, comma-separated (e.g. 'lofi greek pop, melancholic, 90s-influenced, "
        "warm rhodes and vinyl crackle, breathy female vocals, slow'). [Structure] tags like [Verse], [Chorus], "
        "[Bridge], [Instrumental] inside the lyrics box control song structure. Keep the style string under "
        "~120 characters for consistency; long kitchen-sink style strings tend to produce muddier results."
    ),
    "higgsfield": (
        "Higgsfield video prompts are camera-first: shot type (close-up/wide/dolly-in/orbit), lighting "
        "(neon, golden hour, practical), motion (slow push-in, handheld, static), and a short scene "
        "description last. Naming a specific lens/film stock feel (35mm grain, anamorphic) sharpens the look. "
        "State character/scene consistency explicitly if this shot continues a previous one."
    ),
    "openart": (
        "OpenArt prompts benefit from explicit structure: subject, action, environment, style/medium, "
        "lighting, camera angle, then a short negative prompt (what to avoid: extra limbs, text, blur). "
        "Reference a known artist/style sparingly and combine with concrete technical terms (film grain, "
        "depth of field, aspect ratio) rather than vague adjectives."
    ),
}


# Actions here ALWAYS go through propose_confirmed_action → human review before
# anything executes. This is the default and stays the default for anything new.
ACTION_CAPABILITIES = {
    "gmail.send": "gmail.send",
    "calendar.create": "calendar.write",
    "google_tasks.create": "tasks.write",
    "youtube.upload": "youtube.publish",
    "youtube.comment.reply": "youtube.publish",
    "spotify.playback": "spotify.control",
    "spotify.playlist.create": "spotify.control",
    "spotify.saved_tracks.add": "spotify.control",
    "spotify.saved_tracks.remove": "spotify.control",
    "tiktok.publish": "tiktok.publish",
    "instagram.publish": "instagram.publish",
    "homeassistant.service": "homeassistant.control",
    "project_files.write_text": "creative.write",
    "project_files.mkdir": "creative.write",
    "project_files.move": "creative.write",
    "project_files.save_media": "creative.write",
}

# Emby and Asterisk are the two explicit exceptions Tommy asked for: ATHENA
# executes these immediately when its own reasoning proposes them — no
# propose_confirmed_action, no human review step. Still permission-gated
# (emby.control / voip.call) and still audit-logged, just not held for
# confirmation. Every other capability in this file stays confirm-first.
AUTONOMOUS_CAPABILITIES = {
    "emby.control": "emby.control",
    "voip.call": "voip.call",
}


def _tool(name: str, description: str, properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "function", "function": {"name": name, "description": description, "parameters": {"type": "object", "properties": properties, "required": required or [], "additionalProperties": False}}}


def available_tools(user) -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []
    if is_allowed(user, "memory.private"):
        tools += [
            _tool("memory_search", "Search ATHENA semantic memory visible to the current user.", {"query": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 10}}, ["query"]),
            _tool("memory_add_private", "Store a private memory for the current user.", {"text": {"type": "string"}}, ["text"]),
        ]
    if is_allowed(user, "family.tasks.read"):
        tools.append(_tool("family_tasks_list", "List family tasks assigned to or created by the current user.", {"status": {"type": "string", "enum": ["open", "in_progress", "done", "cancelled", "all"]}}))
    if is_allowed(user, "family.tasks.write"):
        tools.append(_tool("family_task_create", "Create a family task for the current user.", {"title": {"type": "string"}, "notes": {"type": "string"}, "due_at": {"type": ["string", "null"]}}, ["title"]))
    if is_allowed(user, "gmail.read"):
        tools.append(_tool("gmail_search", "Search the current user's Gmail account.", {"query": {"type": "string"}, "max_results": {"type": "integer", "minimum": 1, "maximum": 20}}))
    if is_allowed(user, "calendar.read"):
        tools.append(_tool("calendar_upcoming", "List upcoming events in the current user's primary Google Calendar.", {"max_results": {"type": "integer", "minimum": 1, "maximum": 50}}))
    if is_allowed(user, "tasks.read"):
        tools.append(_tool("google_tasks_list", "List the current user's Google Tasks.", {"max_results": {"type": "integer", "minimum": 1, "maximum": 100}}))
    if is_allowed(user, "youtube.read"):
        tools.append(_tool("youtube_channel", "Read the current user's YouTube channel and statistics.", {}))
    if is_allowed(user, "spotify.read"):
        tools.append(_tool("spotify_current", "Read the current user's currently playing Spotify item.", {}))
    if is_allowed(user, "instagram.read"):
        tools.append(_tool("instagram_recent_media", "Read the current user's recent Instagram posts (Professional account required).", {}))
    if is_allowed(user, "homeassistant.read"):
        tools.append(_tool("homeassistant_states", "Read Home Assistant entity states. This tool never changes a device.", {"entity_id": {"type": ["string", "null"]}}))
    if is_allowed(user, "creative.write"):
        tools += [
            _tool(
                "craft_prompt",
                "Get grounding to write or refine a Suno/Higgsfield/OpenArt prompt: platform conventions plus "
                "this user's own past prompts that actually worked. Use the result to write the actual prompt "
                "yourself in your reply — this tool only supplies material, it does not generate the prompt.",
                {"platform": {"type": "string", "enum": ["suno", "higgsfield", "openart"]}, "brief": {"type": "string"}},
                ["platform", "brief"],
            ),
            _tool(
                "creative_project_create",
                "Start tracking a new song/videoclip project.",
                {"title": {"type": "string"}, "kind": {"type": "string", "enum": ["song", "videoclip", "combo", "other"]}, "notes": {"type": "string"}},
                ["title"],
            ),
            _tool(
                "creative_project_update_status",
                "Update the status of an existing project.",
                {"project_id": {"type": "string"}, "status": {"type": "string", "enum": ["idea", "writing", "generating", "mixing", "done", "published", "abandoned"]}},
                ["project_id", "status"],
            ),
            _tool(
                "creative_prompt_log",
                "Log a prompt attempt against a project — always log this after craft_prompt is actually used or tried, including whether it worked.",
                {
                    "project_id": {"type": "string"},
                    "platform": {"type": "string", "enum": ["suno", "higgsfield", "openart", "other"]},
                    "prompt_text": {"type": "string"},
                    "outcome": {"type": "string", "enum": ["untried", "worked", "discarded"]},
                    "notes": {"type": "string"},
                },
                ["project_id", "platform", "prompt_text"],
            ),
            _tool(
                "generate_image",
                "Generate an image (e.g. a character sheet, cover art, or reference image) from a text prompt. "
                "Runs through ATHENA's own brain infrastructure (GPT Image, via the existing ChatGPT connection) — "
                "no separate account or cost. Saves the result to this user's private media library and returns "
                "its media_id.",
                {"prompt": {"type": "string"}, "size": {"type": "string", "enum": ["1024x1024", "1024x1536", "1536x1024"]}},
                ["prompt"],
            ),
        ]
    if is_allowed(user, "creative.read"):
        tools.append(_tool(
            "creative_project_list",
            "List this user's song/videoclip projects.",
            {"status": {"type": "string", "enum": ["idea", "writing", "generating", "mixing", "done", "published", "abandoned", "all"]}},
        ))
        if project_files_mounted():
            tools += [
                _tool(
                    "project_files_search",
                    "List/search real files in the mounted projects share (lyrics, notes, reference files already on disk) — read-only.",
                    {"subpath": {"type": "string"}, "search": {"type": "string"}},
                ),
                _tool(
                    "project_files_read",
                    "Read the text content of one file from the mounted projects share by its path (from project_files_search).",
                    {"path": {"type": "string"}},
                    ["path"],
                ),
            ]
    if is_allowed(user, "routines.manage"):
        tools += [
            _tool(
                "routine_create",
                "Schedule something to happen on its own later — a daily briefing, a reminder, a recurring notification. "
                "trigger_type 'cron' needs trigger_config={cron: '0 7 * * *'}. trigger_type 'interval_minutes' needs "
                "trigger_config={minutes: 30} (minimum 5). action_type 'ask' needs action_config={prompt: '...'} — this "
                "re-runs you with that prompt on schedule and notifies the user with the answer. action_type 'notify' "
                "needs action_config={message: '...'} — sends that exact text, no reasoning involved.",
                {
                    "title": {"type": "string"},
                    "trigger_type": {"type": "string", "enum": ["cron", "interval_minutes"]},
                    "trigger_config": {"type": "object"},
                    "action_type": {"type": "string", "enum": ["ask", "notify"]},
                    "action_config": {"type": "object"},
                },
                ["title", "trigger_type", "trigger_config", "action_type", "action_config"],
            ),
            _tool("routine_list", "List this user's scheduled routines.", {}),
        ]
    if is_allowed(user, "emby.read"):
        tools.append(_tool("emby_sessions", "Read active Emby sessions.", {}))
    if is_allowed(user, "emby.control"):
        tools.append(_tool(
            "emby_control",
            "Control an active Emby playback session immediately. No confirmation step — only call this when you are sure.",
            {
                "session_id": {"type": "string"},
                "command": {"type": "string", "enum": ["PlayPause", "Stop", "Unpause", "Pause", "NextTrack", "PreviousTrack"]},
            },
            ["session_id", "command"],
        ))
    if is_allowed(user, "voip.call"):
        tools.append(_tool(
            "voip_originate_call",
            "Place a call through the Asterisk PBX immediately. No confirmation step — only call this when you are sure.",
            {
                "channel": {"type": "string"},
                "extension": {"type": "string"},
                "context": {"type": ["string", "null"]},
                "caller_id": {"type": ["string", "null"]},
            },
            ["channel", "extension"],
        ))
    permitted_actions = sorted(action for action, capability in ACTION_CAPABILITIES.items() if is_allowed(user, capability))
    if permitted_actions:
        tools.append(_tool(
            "propose_confirmed_action",
            "Prepare a state-changing action for the user to review and confirm in the ATHENA Actions page. This never executes the action.",
            {
                "action": {"type": "string", "enum": permitted_actions},
                "payload": {"type": "object", "additionalProperties": True},
                "summary": {"type": "string", "maxLength": 500},
            },
            ["action", "payload", "summary"],
        ))
    if is_allowed(user, "mcp.use"):
        tools += cached_tools_as_functions()
    return tools


async def execute_tool(user, name: str, args: dict[str, Any]) -> Any:
    if name == "memory_search" and is_allowed(user, "memory.private"):
        namespaces = ["private"]
        if is_allowed(user, "memory.family"):
            namespaces.append("family_shared")
        if is_allowed(user, "memory.project"):
            namespaces.append("project_shared")
        if is_allowed(user, "memory.system"):
            namespaces.append("system")
        try:
            return search_memory(user["id"], args["query"], namespaces=namespaces, limit=int(args.get("limit", 8)))
        except Exception as exc:
            return {"status": "model_unavailable", "error": str(exc)}
    if name == "memory_add_private" and is_allowed(user, "memory.private"):
        try:
            return add_memory(user["id"], "private", args["text"], {"source": "assistant_tool"})
        except Exception as exc:
            return {"status": "model_unavailable", "error": str(exc)}
    if name == "family_tasks_list" and is_allowed(user, "family.tasks.read"):
        status = args.get("status", "open")
        with connect() as conn:
            if status == "all":
                rows = conn.execute("SELECT * FROM family_tasks WHERE created_by=? OR assigned_to=? ORDER BY created_at DESC LIMIT 100", (user["id"], user["id"])).fetchall()
            else:
                rows = conn.execute("SELECT * FROM family_tasks WHERE (created_by=? OR assigned_to=?) AND status=? ORDER BY created_at DESC LIMIT 100", (user["id"], user["id"], status)).fetchall()
        return [dict(r) for r in rows]
    if name == "family_task_create" and is_allowed(user, "family.tasks.write"):
        now = utcnow()
        task_id = str(uuid.uuid4())
        with connect() as conn:
            conn.execute("INSERT INTO family_tasks(id,created_by,assigned_to,title,notes,due_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)", (task_id, user["id"], user["id"], args["title"], args.get("notes", ""), args.get("due_at"), now, now))
        return {"id": task_id, "status": "open"}
    if name == "gmail_search" and is_allowed(user, "gmail.read"):
        return await gmail_list(user["id"], args.get("query", ""), int(args.get("max_results", 10)))
    if name == "calendar_upcoming" and is_allowed(user, "calendar.read"):
        return await calendar_list(user["id"], max_results=int(args.get("max_results", 20)))
    if name == "google_tasks_list" and is_allowed(user, "tasks.read"):
        return await tasks_list(user["id"], max_results=int(args.get("max_results", 50)))
    if name == "youtube_channel" and is_allowed(user, "youtube.read"):
        return await youtube_channel(user["id"])
    if name == "spotify_current" and is_allowed(user, "spotify.read"):
        return await spotify_current(user["id"])
    if name == "instagram_recent_media" and is_allowed(user, "instagram.read"):
        return await instagram_media(user["id"], limit=10)
    if name == "homeassistant_states" and is_allowed(user, "homeassistant.read"):
        entity_id = args.get("entity_id")
        return await homeassistant_request("GET", f"/api/states/{entity_id}" if entity_id else "/api/states")
    if name == "craft_prompt" and is_allowed(user, "creative.write"):
        platform = str(args.get("platform", ""))
        if platform not in PLATFORM_NOTES:
            return {"status": "error", "error": "platform must be suno, higgsfield or openart"}
        return {
            "platform": platform,
            "brief": args.get("brief", ""),
            "platform_notes": PLATFORM_NOTES[platform],
            "prompts_that_worked_before": prompts_that_worked(user["id"], platform=platform, limit=5),
        }
    if name == "creative_project_create" and is_allowed(user, "creative.write"):
        return create_project(user["id"], str(args.get("title", "")), args.get("kind", "song"), args.get("notes", ""))
    if name == "creative_project_update_status" and is_allowed(user, "creative.write"):
        ok = update_project_status(user["id"], str(args.get("project_id", "")), str(args.get("status", "")))
        return {"status": "updated" if ok else "not_found"}
    if name == "creative_prompt_log" and is_allowed(user, "creative.write"):
        try:
            return add_prompt(
                user["id"],
                str(args.get("project_id", "")),
                str(args.get("platform", "")),
                str(args.get("prompt_text", "")),
                args.get("outcome", "untried"),
                args.get("notes", ""),
            )
        except (ValueError, PermissionError) as exc:
            return {"status": "error", "error": str(exc)}
    if name == "generate_image" and is_allowed(user, "creative.write"):
        prompt = str(args.get("prompt", "")).strip()
        if not prompt:
            return {"status": "error", "error": "prompt is required"}
        size = args.get("size") or "1024x1024"
        if size not in {"1024x1024", "1024x1536", "1536x1024"}:
            size = "1024x1024"
        try:
            response = await generate_image(prompt, size=size)
        except CLIProxyError as exc:
            return {"status": "error", "error": exc.message}
        if response.status_code != 200:
            return {"status": "error", "error": f"image generation failed ({response.status_code}): {response.text[:300]}"}
        try:
            item = response.json()["data"][0]
            image_bytes = base64.b64decode(item["b64_json"]) if "b64_json" in item else None
        except (KeyError, IndexError, ValueError):
            return {"status": "error", "error": "unexpected response shape from image generation"}
        if image_bytes is None:
            return {"status": "error", "error": "image generation returned a URL instead of image data — not saved"}
        media_id = str(uuid.uuid4())
        user_dir = MEDIA_DIR / "users" / user["id"]
        user_dir.mkdir(parents=True, exist_ok=True)
        target = user_dir / f"{media_id}.png"
        target.write_bytes(image_bytes)
        with connect() as conn:
            conn.execute(
                "INSERT INTO media_files(id,owner_id,original_name,relative_path,media_type,size_bytes,created_at) VALUES(?,?,?,?,?,?,?)",
                (media_id, user["id"], f"{prompt[:60]}.png", target.relative_to(MEDIA_DIR).as_posix(), "image/png", len(image_bytes), utcnow()),
            )
        return {"status": "ready", "media_id": media_id, "prompt": prompt, "size": size}
    if name == "creative_project_list" and is_allowed(user, "creative.read"):
        return list_projects(user["id"], args.get("status", "all"))
    if name == "project_files_search" and is_allowed(user, "creative.read"):
        return list_project_files(args.get("subpath", ""), args.get("search", ""))
    if name == "project_files_read" and is_allowed(user, "creative.read"):
        return read_project_file(str(args.get("path", "")))
    if name == "routine_create" and is_allowed(user, "routines.manage"):
        try:
            routine = create_routine_row(
                user["id"],
                str(args.get("title", "")),
                str(args.get("trigger_type", "")),
                args.get("trigger_config") or {},
                str(args.get("action_type", "")),
                args.get("action_config") or {},
                source="athena_proposed",
            )
        except ValueError as exc:
            return {"status": "error", "error": str(exc)}
        schedule_routine(routine)
        return {"status": "scheduled", "routine": routine}
    if name == "routine_list" and is_allowed(user, "routines.manage"):
        return list_routines(user["id"])
    if name == "emby_sessions" and is_allowed(user, "emby.read"):
        return await emby_request("GET", "/Sessions")
    if name == "emby_control" and is_allowed(user, "emby.control"):
        session_id = str(args.get("session_id", ""))
        command = str(args.get("command", ""))
        if command not in {"PlayPause", "Stop", "Unpause", "Pause", "NextTrack", "PreviousTrack"} or not session_id:
            return {"status": "error", "error": "Invalid Emby session_id or command"}
        result = await emby_request("POST", f"/Sessions/{session_id}/Playing/{command}")
        audit(user["id"], "emby_control_autonomous", {"session_id": session_id, "command": command}, "")
        return {"status": "executed", "result": result}
    if name == "voip_originate_call" and is_allowed(user, "voip.call"):
        payload = {
            "channel": str(args.get("channel", "")),
            "extension": str(args.get("extension", "")),
        }
        if args.get("context"):
            payload["context"] = str(args["context"])
        if args.get("caller_id"):
            payload["caller_id"] = str(args["caller_id"])
        if not payload["channel"] or not payload["extension"]:
            return {"status": "error", "error": "channel and extension are required"}
        result = await asterisk_originate(payload)
        audit(user["id"], "voip_call_autonomous", {"channel": payload["channel"], "extension": payload["extension"], "response": result.get("Response")}, "")
        return {"status": "executed", "result": result}
    if name == "propose_confirmed_action":
        action = str(args.get("action", ""))
        capability = ACTION_CAPABILITIES.get(action)
        payload = args.get("payload")
        summary = str(args.get("summary", "")).strip()[:500]
        if not capability or not is_allowed(user, capability) or not isinstance(payload, dict):
            return {"status": "permission_denied", "error": "The proposed action is not permitted"}
        proposal_id = str(uuid.uuid4())
        now = utcnow()
        with connect() as conn:
            conn.execute(
                "INSERT INTO action_proposals(id,user_id,action,payload_json,status,result_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (proposal_id, user["id"], action, json.dumps({"payload": payload, "summary": summary}, ensure_ascii=False), "pending", None, now, now),
            )
        return {"status": "confirmation_required", "proposal_id": proposal_id, "action": action, "summary": summary, "review_url": "/actions"}
    if name.startswith("mcp_") and is_allowed(user, "mcp.use"):
        if mcp_tool_requires_confirmation(name):
            proposal_id = str(uuid.uuid4())
            now = utcnow()
            with connect() as conn:
                conn.execute(
                    "INSERT INTO action_proposals(id,user_id,action,payload_json,status,result_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (proposal_id, user["id"], name, json.dumps({"payload": args, "summary": f"MCP tool: {name}"}, ensure_ascii=False), "pending", None, now, now),
                )
            return {"status": "confirmation_required", "proposal_id": proposal_id, "action": name, "review_url": "/actions"}
        return await call_cached_tool(name, args)
    return {"status": "permission_denied", "error": "Tool is not available to this user"}


async def _chat_with_failover(base_payload: dict[str, Any], model_order: list[str]) -> tuple[httpx.Response, str, list[dict[str, Any]]]:
    failures: list[dict[str, Any]] = []
    for model in model_order:
        payload = dict(base_payload)
        payload["model"] = model
        try:
            response = await chat_completions(payload)
        except CLIProxyError as exc:
            failures.append({"model_family": model_family(model), "status": exc.status, "error": exc.message})
            continue
        if response.status_code < 400:
            return response, model, failures
        failure = {
            "model_family": model_family(model),
            "status": "provider_http_error",
            "provider_http_status": response.status_code,
            "error": response.text[:500],
        }
        failures.append(failure)
        if response.status_code in {401, 403}:
            break
    raise CLIProxyError(
        "provider_unavailable",
        "Όλα τα διαθέσιμα τμήματα του εγκεφάλου LLM απέτυχαν για αυτό το αίτημα.",
        details={"attempts": failures},
    )


async def ask(user, question: str) -> dict[str, Any]:
    try:
        route = await automatic_route(question)
    except CLIProxyError as exc:
        return {"status": exc.status, "answer": exc.message, "details": exc.details}

    memories = []
    try:
        namespaces = ["private"]
        if is_allowed(user, "memory.family"):
            namespaces.append("family_shared")
        if is_allowed(user, "memory.project"):
            namespaces.append("project_shared")
        if is_allowed(user, "memory.system"):
            namespaces.append("system")
        memories = search_memory(user["id"], question, namespaces=namespaces, limit=6)
    except Exception as exc:
        memories = [{"status": "model_unavailable", "error": str(exc)}]

    with connect() as conn:
        history_rows = conn.execute(
            "SELECT role,content FROM conversations WHERE user_id=? ORDER BY created_at DESC LIMIT 12",
            (user["id"],),
        ).fetchall()
    history = [
        {"role": row["role"], "content": row["content"]}
        for row in reversed(history_rows)
        if row["role"] in {"user", "assistant"}
    ]
    system = (
        "Είσαι η ATHENA, η ιδιωτική βοηθός της οικογένειας του Tommy. Ο Tommy είναι μουσικός παραγωγός — "
        "δουλεύει με Suno για μουσική και φτιάχνει AI videoclips με Higgsfield και OpenArt. "
        "Όταν μιλάς στον Tommy για μουσική/videoclip δουλειά, μίλα σαν δημιουργικός, ενθουσιώδης κολλητός — "
        "όχι ξερές απαντήσεις, μπες στην ιδέα μαζί του. Με τα υπόλοιπα μέλη της οικογένειας μίλα φυσικά, ανάλογα το πλαίσιο. "
        "Απάντησε στα ελληνικά εκτός αν ζητηθεί άλλη γλώσσα. "
        "Ο εγκέφαλος της ATHENA αποτελείται από τις τέσσερις οικογένειες Claude, Codex/OpenAI, Gemini και Grok μέσω router-for-me/CLIProxyAPI. "
        "Η επιλογή μοντέλου γίνεται αποκλειστικά και εσωτερικά από την ATHENA. Μην ζητήσεις ποτέ από τον χρήστη να επιλέξει μοντέλο και μην παρουσιάσεις επιλογέα μοντέλου. "
        "Χρησιμοποίησε μόνο εργαλεία που δίνονται. Μην ισχυρίζεσαι ότι εκτελέστηκε ενέργεια αν το εργαλείο απέτυχε. Μην επινοήσεις ποτέ αριθμό, ημερομηνία ή γεγονός. "
        "Πρόσεξε: το creative_project_list και το project_files_search/project_files_read είναι δύο ΤΕΛΕΙΩΣ διαφορετικά πράγματα — μην τα μπερδεύεις. "
        "Το creative_project_list δείχνει επίσημα καταχωρημένα projects στη βάση (status/platform) — μπορεί να είναι κενό ακόμα κι όταν υπάρχουν χιλιάδες πραγματικά αρχεία στον δίσκο, αυτό είναι φυσιολογικό, όχι σφάλμα. "
        "Το project_files_search/project_files_read είναι ο πραγματικός κοινόχρηστος φάκελος στον δίσκο (lyrics, storyboards, mp3, κλπ). "
        "Όταν ο χρήστης ρωτήσει για 'αρχεία', 'φάκελο', 'projects' με την έννοια των πραγματικών αρχείων του, κάλεσε ΠΑΝΤΑ project_files_search φρέσκο σε αυτό το turn — ποτέ μην απαντήσεις 'είναι άδειο' βασισμένο σε παλιότερη μνήμη της συζήτησης ή στο creative_project_list. "
        "Αν σου ζητηθεί \"διαγνωστικό\" ή \"έλεγχος συστήματος\": κάλεσε πραγματικά κάθε εργαλείο που αναφέρεις — ποτέ μην γράψεις 'OK' ή κατάσταση ενός εργαλείου (ειδικά voip_originate_call ή emby_control, που εκτελούνται αμέσως χωρίς επιβεβαίωση) χωρίς να το έχεις πράγματι καλέσει. Αν δεν έχει νόημα να καλέσεις κάτι (π.χ. θα ξεκινούσε πραγματική κλήση χωρίς λόγο), πες το ρητά αντί να το παραστήσεις. "
        "Οι απαντήσεις σου διαβάζονται και φωναχτά (text-to-speech) — μίλα σε φυσική ελληνική πεζογραφία, όχι λίστες με bullets/markdown/αγγλικούς τεχνικούς όρους σαν 'OK'/'FAIL'. Αν χρειάζεται να αναφέρεις κατάσταση πολλών πραγμάτων, πες το σαν πρόταση, όχι σαν πίνακα. Απόφυγε αγγλικές λέξεις μέσα σε ελληνική πρόταση όταν υπάρχει φυσικός ελληνικός τρόπος να το πεις — η φωνή που σε διαβάζει είναι ελληνική και τα αγγλικά μέσα σε ελληνικό κείμενο ακούγονται παραμορφωμένα. "
        "Ενέργειες όπως αποστολή email, δημοσίευση σε YouTube/Spotify/TikTok/Instagram, δημιουργία/αλλαγή calendar ή εντολή σε Home Assistant "
        "απαιτούν ξεχωριστή επιβεβαίωση από τον χρήστη πριν εκτελεστούν — χρησιμοποίησε το propose_confirmed_action. "
        "Ο έλεγχος του Emby (emby_control) και η κλήση μέσω Asterisk (voip_originate_call) ΔΕΝ χρειάζονται επιβεβαίωση — εκτελούνται αμέσως όταν τα καλέσεις, οπότε κάλεσέ τα μόνο όταν είσαι σίγουρη τι ζητήθηκε. "
        f"Τρέχων χρήστης: {user['display_name']} ({user['role']}). "
        f"Σχετικές μνήμες: {json.dumps(memories, ensure_ascii=False)[:6000]}"
    )
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        *history,
        {"role": "user", "content": question},
    ]
    tools = available_tools(user)
    with connect() as conn:
        conn.execute(
            "INSERT INTO conversations(id,user_id,role,content,created_at) VALUES(?,?,?,?,?)",
            (str(uuid.uuid4()), user["id"], "user", question, utcnow()),
        )

    model_order = [route["primary_model"], *route["fallback_models"]]
    active_model = model_order[0]
    all_failures: list[dict[str, Any]] = []
    for _ in range(6):
        request_body: dict[str, Any] = {"messages": messages, "temperature": 0.2}
        if tools:
            request_body["tools"] = tools
            request_body["tool_choice"] = "auto"
        current_order = [active_model, *[model for model in model_order if model != active_model]]
        try:
            response, active_model, failures = await _chat_with_failover(request_body, current_order)
            all_failures.extend(failures)
        except CLIProxyError as exc:
            return {
                "status": exc.status,
                "answer": exc.message,
                "details": exc.details,
                "brain": {
                    "selection": "athena_automatic",
                    "route_family": route["primary_family"],
                    "brain_status": route["brain_status"],
                },
            }
        try:
            data = response.json()
            choice = data["choices"][0]["message"]
        except (ValueError, KeyError, IndexError, TypeError):
            return {
                "status": "error",
                "answer": "CLIProxyAPI επέστρεψε μη έγκυρη απάντηση chat-completions.",
                "details": response.text[:2000],
                "brain": {"selection": "athena_automatic", "route_family": model_family(active_model)},
            }
        assistant_message: dict[str, Any] = {"role": "assistant", "content": choice.get("content") or ""}
        tool_calls = choice.get("tool_calls") or []
        if tool_calls:
            assistant_message["tool_calls"] = tool_calls
        messages.append(assistant_message)
        if not tool_calls:
            answer = choice.get("content") or ""
            with connect() as conn:
                conn.execute(
                    "INSERT INTO conversations(id,user_id,role,content,created_at) VALUES(?,?,?,?,?)",
                    (str(uuid.uuid4()), user["id"], "assistant", answer, utcnow()),
                )
            return {
                "status": "ready",
                "answer": answer,
                "brain": {
                    "selection": "athena_automatic",
                    "route_family": model_family(active_model),
                    "reason": route["reason"],
                    "brain_status": route["brain_status"],
                    "failover_count": len(all_failures),
                },
            }
        for call in tool_calls:
            function = call.get("function", {})
            try:
                args = json.loads(function.get("arguments") or "{}")
                result = await execute_tool(user, function.get("name", ""), args)
            except Exception as exc:
                result = {"status": "error", "error": str(exc)}
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id", ""),
                    "content": json.dumps(result, ensure_ascii=False, default=str)[:12000],
                }
            )
    return {
        "status": "error",
        "answer": "Το όριο κύκλων εργαλείων εξαντλήθηκε χωρίς τελική απάντηση.",
        "brain": {"selection": "athena_automatic", "route_family": model_family(active_model)},
    }
