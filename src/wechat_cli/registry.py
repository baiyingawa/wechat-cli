from dataclasses import dataclass

from .errors import AutomationError


@dataclass(frozen=True)
class Method:
    handler: str | None
    description: str
    properties: dict
    required: tuple = ()
    mutation: bool = False
    destructive: bool = False
    verification: str = "unverified"

    def schema(self):
        return {"type": "object", "properties": self.properties,
                "required": list(self.required), "additionalProperties": False}


CHAT = {"type": "string", "minLength": 1, "maxLength": 200}
TEXT = {"type": "string", "minLength": 1, "maxLength": 10000}
METHODS = {
    "doctor": Method("doctor", "Inspect display, dependencies and recognition backends", {}),
    "session.status": Method("session_status", "Inspect visible client login state", {}),
    "session.start": Method("session_start", "Start virtual display and WeChat if needed", {}, mutation=True),
    "session.login": Method("session_login", "Start manual phone login and return a local screenshot path", {}, mutation=True),
    "session.logout": Method("session_logout", "Log out through the native client confirmation", {}, mutation=True),
    "session.remote": Method("session_remote", "Start local-only VNC with a password for SSH tunneling", {}, mutation=True),
    "session.gui": Method("session_gui", "Enable or disable the local Windows-accessible VNC GUI", {
        "enabled": {"type": "boolean"}}, ("enabled",), mutation=True),
    "ui.windows": Method("ui_windows", "List current WeChat windows", {}),
    "ui.tree": Method("ui_tree", "Inspect optional accessibility tree", {}),
    "ui.screenshot": Method("screenshot", "Save a local screenshot", {
        "region": {"type": "array", "items": {"type": "integer"}, "minItems": 4, "maxItems": 4},
        "window_id": {"type": "integer", "minimum": 1}}),
    "ui.maximize": Method("ui_maximize", "Fit client to virtual display", {}, mutation=True),
    "ui.reset": Method("ui_reset", "Close transient panels and restore the reusable chat surface", {}, mutation=True),
    "account.profile": Method("account_profile", "Read the locally cached account profile without UI interaction", {}),
    "account.refresh": Method("account_refresh", "Refresh and persist account name, WeChat ID, and opened avatar subject to a two-hour rate limit", {}, mutation=True),
    "chat.open": Method("chat_open", "Open an exact display-name match and verify header",
                        {"chat": CHAT}, ("chat",), mutation=True),
    "chat.list": Method("chat_list", "Read visible conversation rows via OCR", {}),
    "chat.pin": Method("chat_pin", "Set conversation pin state", {"chat": CHAT,
        "enabled": {"type": "boolean"}}, ("chat", "enabled"), mutation=True),
    "chat.mute": Method("chat_mute", "Set conversation do-not-disturb state", {"chat": CHAT,
        "enabled": {"type": "boolean"}}, ("chat", "enabled"), mutation=True),
    "message.read": Method("message_read", "Read visible bubbles or scroll for up to 30",
        {"chat": CHAT, "limit": {"type": "integer", "minimum": 1, "maximum": 30}}, ("chat",)),
    "message.search": Method("message_search", "Search text within up to 30 recent visually read messages",
        {"chat": CHAT, "query": TEXT,
         "limit": {"type": "integer", "minimum": 1, "maximum": 30}},
        ("chat", "query")),
    "history.manage": Method("history_manage", "Report local retention and supported history operations", {}),
    "message.send": Method("message_send", "Submit text; requires an idempotency key",
                           {"chat": CHAT, "text": TEXT}, ("chat", "text"), mutation=True),
    "message.send_file": Method("message_send_file", "Send an existing local file; requires an idempotency key",
        {"chat": CHAT, "path": {"type": "string", "minLength": 1, "maxLength": 4096}},
        ("chat", "path"), mutation=True),
    "message.download": Method("message_download", "Save one visible attachment by visual digest",
        {"chat": CHAT, "visual_digest": {"type": "string", "minLength": 32, "maxLength": 32},
         "path": {"type": "string", "minLength": 1, "maxLength": 4096}},
        ("chat", "visual_digest", "path"), mutation=True),
    "message.reply": Method("message_reply", "Quote a unique visible outgoing message and send text",
        {"chat": CHAT, "quote_text": TEXT, "text": TEXT},
        ("chat", "quote_text", "text"), mutation=True),
    "message.forward": Method("message_forward", "Forward one unique outgoing visible message to one exact recipient",
        {"chat": CHAT, "text": TEXT, "to": CHAT}, ("chat", "text", "to"), mutation=True),
    "message.revoke": Method("message_revoke", "Recall an exact, unique visible outgoing text message",
                             {"chat": CHAT, "text": TEXT}, ("chat", "text"), mutation=True),
    "message.pat": Method("message_pat", "Pat the latest visible self or other avatar; requires an idempotency key",
                          {"chat": CHAT, "target": {"type": "string", "enum": ["other", "self"]}},
                          ("chat",), mutation=True),
    "message.pat_revoke": Method("message_pat_revoke", "Recall the latest visible own Pat; requires an idempotency key",
                                  {"chat": CHAT}, ("chat",), mutation=True),
    "message.delete": Method("message_delete", "Delete one exact visible outgoing text message after explicit confirmation",
        {"chat": CHAT, "text": TEXT}, ("chat", "text"), mutation=True, destructive=True),
    "message.voice_text": Method("message_voice_text", "Convert one visible voice bubble to text by visual digest",
        {"chat": CHAT, "visual_digest": {"type": "string", "minLength": 32, "maxLength": 32}},
        ("chat", "visual_digest"), mutation=True),
    "contact.info": Method("contact_info", "Read profile fields as visual observations",
                           {"chat": CHAT}, ("chat",)),
    "contact.remark": Method("contact_remark", "Set or clear a contact remark through the profile card",
        {"chat": CHAT, "remark": {"type": "string", "maxLength": 64}},
        ("chat", "remark"), mutation=True),
    "contact.list": Method("contact_list", "Read visible contacts in the address-book panel", {}, mutation=True),
    "contact.add": Method("contact_add", "Search a WeChat ID and submit a friend request; requires an idempotency key",
                           {"identifier": CHAT, "message": {"type": "string", "maxLength": 200}},
                           ("identifier",), mutation=True),
    "contact.accept": Method("contact_accept", "Accept one exact visible new-friend request; requires an idempotency key",
                              {"requester": CHAT}, ("requester",), mutation=True),
    "contact.delete": Method("contact_delete", "Delete a contact after explicit confirmation",
                              {"chat": CHAT}, ("chat",), mutation=True, destructive=True),
    "favorite.list": Method("favorite_list", "Read visible favorites and optional category", {
        "category": {"type": "string", "minLength": 1, "maxLength": 20}}, mutation=True),
    "favorite.search": Method("favorite_search", "Search favorites using the visible search box", {
        "query": TEXT}, ("query",), mutation=True),
    "favorite.add": Method("favorite_add", "Favorite a unique visible outgoing text message", {
        "chat": CHAT, "text": TEXT}, ("chat", "text"), mutation=True),
    "favorite.delete": Method("favorite_delete", "Delete one exact searched favorite after explicit confirmation", {
        "text": TEXT}, ("text",), mutation=True, destructive=True),
    "group.members": Method("group_members", "Read visible members in an expanded group panel",
        {"chat": CHAT}, ("chat",), mutation=True),
    "group.announcement": Method("group_announcement", "Read visible group announcement summary",
        {"chat": CHAT}, ("chat",), mutation=True),
    "group.leave": Method("group_leave", "Leave an exact group without clearing chat history after explicit confirmation",
        {"chat": CHAT}, ("chat",), mutation=True, destructive=True),
    "group.rename": Method("group_rename", "Set a group name; requires an idempotency key",
                             {"chat": CHAT, "name": {"type": "string", "minLength": 1, "maxLength": 64}},
                             ("chat", "name"), mutation=True),
    "group.announcement_set": Method("group_announcement_set", "Publish a group announcement; requires an idempotency key",
                                        {"chat": CHAT, "text": {"type": "string", "minLength": 1, "maxLength": 2000}},
                                        ("chat", "text"), mutation=True),
    "group.create": Method("group_create", "Create a group from 2-200 contacts; requires an idempotency key",
                            {"contacts": {"type": "array", "items": CHAT, "minItems": 2, "maxItems": 200}},
                            ("contacts",), mutation=True),
    "group.invite": Method("group_invite", "Invite 1-200 contacts to a group; requires an idempotency key",
                            {"chat": CHAT, "contacts": {"type": "array", "items": CHAT, "minItems": 1, "maxItems": 200}},
                            ("chat", "contacts"), mutation=True),
    "group.remove": Method("group_remove", "Remove one member after explicit confirmation",
                            {"chat": CHAT, "member": CHAT}, ("chat", "member"), mutation=True,
                            destructive=True),
    "settings.voice_text": Method("settings_voice_text", "Inspect and enable automatic voice-to-text", {}, mutation=True),
    "moments.open": Method("moments_open", "Open and enlarge contact Moments",
                           {"chat": CHAT}, ("chat",), mutation=True),
    "moments.read": Method("moments_read", "Read visible contact Moments via OCR",
                           {"chat": CHAT}, ("chat",)),
    "moments.feed.open": Method("moments_feed_open", "Open and enlarge the global Moments feed", {}, mutation=True),
    "moments.feed.read": Method("moments_feed_read", "Read currently visible global Moments feed content via OCR", {}),
    "moments.pinned.read": Method("moments_pinned_read", "Open the visible global pinned-Moments row and read it via OCR", {}, mutation=True),
    "moments.like": Method("moments_like", "Like a unique visible post; leave existing likes unchanged",
                           {"chat": CHAT, "post_text": TEXT}, ("chat", "post_text"), mutation=True),
    "moments.unlike": Method("moments_unlike", "Remove a like from a unique visible post; leave absent likes unchanged",
                             {"chat": CHAT, "post_text": TEXT}, ("chat", "post_text"), mutation=True),
    "moments.comment": Method("moments_comment", "Comment on a unique visible post; requires an idempotency key",
                              {"chat": CHAT, "post_text": TEXT, "text": TEXT},
                              ("chat", "post_text", "text"), mutation=True),
    "moments.comment_delete": Method("moments_comment_delete", "Delete one exact own Moments comment after explicit confirmation",
                                     {"chat": CHAT, "post_text": TEXT, "text": TEXT},
                                     ("chat", "post_text", "text"), mutation=True, destructive=True),
    "moments.pin": Method("moments_pin", "Set a unique post in the current account's Moments as pinned or unpinned",
                          {"post_text": TEXT, "enabled": {"type": "boolean"}},
                          ("post_text", "enabled"), mutation=True),
    "moments.publish": Method("moments_publish", "Publish text with one image using an explicit visibility scope; requires an idempotency key",
                              {"text": {"type": "string", "maxLength": 10000},
                               "images": {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 4096}, "minItems": 1, "maxItems": 1},
                               "visibility": {"type": "string", "enum": ["private", "contacts", "public"]},
                               "visible_tags": {"type": "array", "items": CHAT, "maxItems": 100},
                               "visible_contacts": {"type": "array", "items": CHAT, "maxItems": 100},
                              "hidden_contacts": {"type": "array", "items": CHAT, "maxItems": 100}},
                              ("images",), mutation=True),
    "transfer.accept": Method("transfer_accept", "Receive one exact visible incoming transfer; requires an idempotency key",
                              {"chat": CHAT, "text": TEXT}, ("chat", "text"), mutation=True),
}

PLANNED = {
}

SYSTEM_METHODS = (
    {"method": "metrics", "status": "implemented", "verification": "local",
     "description": "Read local automation timing and OCR metrics",
     "params_schema": {"type": "object", "properties": {}, "required": [],
                       "additionalProperties": False},
     "mutation": False, "destructive": False, "idempotency_required": False},
    {"method": "state.cleanup", "status": "implemented", "verification": "local",
     "description": "Remove wechat-cli screenshots and logs older than the configured retention period",
     "params_schema": {"type": "object", "properties": {}, "required": [],
                       "additionalProperties": False},
     "mutation": True, "destructive": False, "idempotency_required": False},
)


def capabilities():
    implemented = [{"method": name, "status": "implemented", "verification": method.verification,
                    "description": method.description, "params_schema": method.schema(),
                    "mutation": method.mutation, "destructive": method.destructive,
                    "idempotency_required": method.destructive or name in ("message.send", "message.send_file", "message.download", "message.reply", "message.forward", "message.revoke", "message.pat", "message.pat_revoke", "favorite.add", "moments.comment", "moments.publish", "contact.add", "contact.accept", "group.create", "group.invite", "group.rename", "group.announcement_set", "transfer.accept")}
                   for name, method in METHODS.items()]
    planned = [{"method": name, "status": "planned", "description": description}
               for name, description in PLANNED.items()]
    return {"protocol_version": 1, "methods": list(SYSTEM_METHODS) + implemented + planned,
            "excluded": ["mini_programs", "channels", "official_accounts", "calls"],
            "deferred": ["multi_account", "watch", "auto_reply", "export"]}


def validate(params, method):
    if not isinstance(params, dict):
        raise AutomationError("INVALID_PARAMS", "params must be an object")
    unknown = set(params) - set(method.properties)
    if unknown:
        raise AutomationError("INVALID_PARAMS", "Unknown parameters", {"parameters": sorted(unknown)})
    missing = set(method.required) - set(params)
    if missing:
        raise AutomationError("INVALID_PARAMS", "Missing parameters", {"parameters": sorted(missing)})
    for name, value in params.items():
        schema = method.properties[name]
        kind = schema["type"]
        valid = {"string": lambda: isinstance(value, str),
                 "integer": lambda: type(value) is int,
                 "boolean": lambda: type(value) is bool,
                 "array": lambda: isinstance(value, list)}[kind]()
        if not valid:
            raise AutomationError("INVALID_PARAMS", f"{name} must be {kind}")
        if kind == "string" and not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", 100000):
            raise AutomationError("INVALID_PARAMS", f"{name} has invalid length")
        if kind == "string" and "enum" in schema and value not in schema["enum"]:
            raise AutomationError("INVALID_PARAMS", f"{name} is not an allowed value")
        if kind == "integer" and not schema.get("minimum", -2**63) <= value <= schema.get("maximum", 2**63):
            raise AutomationError("INVALID_PARAMS", f"{name} is outside allowed range")
        if kind == "array":
            if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", 10000):
                raise AutomationError("INVALID_PARAMS", f"{name} has invalid item count")
            item_schema = schema.get("items", {})
            if item_schema.get("type") == "integer" and any(type(item) is not int for item in value):
                raise AutomationError("INVALID_PARAMS", f"{name} items must be integers")
            if item_schema.get("type") == "string" and any(
                    not isinstance(item, str)
                    or not item_schema.get("minLength", 0) <= len(item) <= item_schema.get("maxLength", 100000)
                    for item in value):
                raise AutomationError("INVALID_PARAMS", f"{name} items must be valid strings")
    return params
