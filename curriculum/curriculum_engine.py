"""TAISE-Agent v0.5 - Curriculum Delivery Engine"""
import json, os
from datetime import datetime, timezone
from pathlib import Path
import yaml

class CurriculumEngine:
    def __init__(self, curriculum_dir="curriculum"):
        self.curriculum_dir = Path(curriculum_dir)
        self.modules_dir = self.curriculum_dir / "modules"
        self.manifest = self._load_manifest()
        self.modules = self._load_modules()

    def _load_manifest(self):
        with open(self.curriculum_dir / "curriculum_manifest.yaml") as f:
            return yaml.safe_load(f)

    def _load_modules(self):
        modules = []
        for mod_info in self.manifest.get("modules", []):
            fp = self.curriculum_dir / mod_info["file"]
            if fp.exists():
                content = fp.read_text()
                if content.startswith("---"):
                    parts = content.split("---", 2)
                    frontmatter = yaml.safe_load(parts[1]) if len(parts) >= 3 else {}
                    body = parts[2].strip() if len(parts) >= 3 else content
                else:
                    frontmatter, body = {}, content
                modules.append({"module_id": mod_info["module_id"], "title": mod_info["title"],
                    "policies": mod_info["policies"], "frontmatter": frontmatter, "content": body})
        return modules

    def generate_study_guide(self, delivery_mode="document_upload"):
        if delivery_mode == "system_prompt":
            parts = ["=== TAISE-Agent Safety Curriculum ===\n"]
            for m in self.modules:
                parts.append(f"\n## {m['title']}\n\n{m['content']}")
            return "\n".join(parts)
        elif delivery_mode == "api_payload":
            return json.dumps({"curriculum_version": self.manifest.get("curriculum_version", "0.5"),
                "modules": [{"module_id": m["module_id"], "title": m["title"],
                    "policies": m["policies"], "content": m["content"]} for m in self.modules]}, indent=2)
        else:
            parts = ["# TAISE-Agent Safety Curriculum Study Guide\n",
                "**Cloud Security Alliance AI Safety Initiative**\n",
                f"**Version: {self.manifest.get('curriculum_version', '0.5')}**\n\n---\n"]
            for m in self.modules:
                parts.append(f"\n# {m['module_id']}: {m['title']}\n\n**Policies:** {', '.join(m['policies'])}\n\n{m['content']}\n\n---\n")
            return "\n".join(parts)

    def deliver_curriculum(self, agent_profile):
        agent_type = agent_profile.get("agent_type", "chat")
        delivery_mode = agent_profile.get("curriculum_delivery", "")
        if not delivery_mode or delivery_mode == "auto":
            delivery_mode = "api_payload" if agent_type in ("mcp", "api") else "document_upload"
        study_guide = self.generate_study_guide(delivery_mode)
        now = datetime.now(timezone.utc).isoformat()
        modules_delivered = [{"module_id": m["module_id"], "title": m["title"],
            "delivered_at": now, "delivery_status": "confirmed",
            "policies_covered": m["policies"]} for m in self.modules]
        all_policies = [p for m in self.modules for p in m["policies"]]
        return {"agent_name": agent_profile.get("agent_name", "Unknown"),
            "delivery_mode": delivery_mode, "modules_delivered": modules_delivered,
            "all_modules_delivered": len(modules_delivered) == len(self.modules),
            "curriculum_version": self.manifest.get("curriculum_version", "0.5"),
            "total_policies_covered": len(all_policies),
            "study_guide_content": study_guide, "delivered_at": now}

    def save_curriculum_record(self, record, output_dir):
        os.makedirs(output_dir, exist_ok=True)
        save_record = {k: v for k, v in record.items() if k != "study_guide_content"}
        filepath = os.path.join(output_dir, "curriculum_record.json")
        with open(filepath, "w") as f:
            json.dump(save_record, f, indent=2)
        return filepath
