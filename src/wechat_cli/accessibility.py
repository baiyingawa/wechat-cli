import time


class Accessibility:
    def __init__(self):
        self.api = None
        self.reason = None
        try:
            import gi
            gi.require_version("Atspi", "2.0")
            from gi.repository import Atspi
            Atspi.set_timeout(100, 250)
            self.api = Atspi
        except (ImportError, ValueError) as error:
            self.reason = str(error)

    def snapshot(self, limit=300, budget_ms=100):
        if self.api is None:
            return {"available": False, "reason": self.reason, "nodes": []}
        deadline = time.monotonic() + budget_ms / 1000
        nodes = []
        truncated = False
        try:
            desktop = self.api.get_desktop(0)
            apps = []
            for index in range(desktop.get_child_count()):
                app = desktop.get_child_at_index(index)
                if "wechat" in app.get_name().lower() or "微信" in app.get_name():
                    apps.append(app)
            queue = [(app, []) for app in apps]
            while queue:
                if len(nodes) >= limit or time.monotonic() >= deadline:
                    truncated = True
                    break
                node, path = queue.pop(0)
                item = {"path": path, "name": node.get_name(), "role": node.get_role_name()}
                try:
                    component = node.get_component_iface()
                    if component:
                        rect = component.get_extents(self.api.CoordType.SCREEN)
                        item["rect"] = [rect.x, rect.y, rect.width, rect.height]
                    text = node.get_text_iface()
                    if text:
                        item["text"] = text.get_text(0, min(text.get_character_count(), 4096))
                except Exception:
                    pass
                nodes.append(item)
                for index in range(min(node.get_child_count(), limit - len(nodes))):
                    child = node.get_child_at_index(index)
                    if child:
                        queue.append((child, path + [index]))
            return {"available": bool(apps), "nodes": nodes, "truncated": truncated,
                    "reason": None if apps else "Client exposes no accessible application"}
        except Exception as error:
            return {"available": False, "reason": str(error), "nodes": nodes}
