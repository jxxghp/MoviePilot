import tempfile
import unittest
from pathlib import Path

from anyio import Path as AsyncPath

from app.agent.middleware.skills import _alist_skills


class SkillsMiddlewareAsyncTest(unittest.IsolatedAsyncioTestCase):
    async def test_alist_skills_sorts_skill_directories_by_name(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)

            for skill_id in ("z-skill", "a-skill", "m-skill"):
                skill_dir = root / skill_id
                skill_dir.mkdir()
                (skill_dir / "SKILL.md").write_text(
                    f"""---
name: {skill_id}
description: test
---
# {skill_id}
""",
                    encoding="utf-8",
                )

            skills = await _alist_skills(AsyncPath(str(root)))

        self.assertEqual(
            ["a-skill", "m-skill", "z-skill"],
            [skill["id"] for skill in skills],
        )


if __name__ == "__main__":
    unittest.main()
