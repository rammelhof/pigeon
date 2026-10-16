"""Section 13: bundled skills are copied into the pi config at startup —
new skills added, changed ones updated, user skills left untouched.
"""
import os
import shutil
import tempfile

from pigeon.skills import sync_skills
from harness import PI_CFG, ROOT, check


def run():
    skills_src = tempfile.mkdtemp()
    skills_dest = os.path.join(PI_CFG, "skills")
    user_skill = os.path.join(skills_dest, "user-skill")
    os.makedirs(user_skill)
    with open(os.path.join(user_skill, "SKILL.md"), "w") as f:
        f.write("---\nname: user-skill\ndescription: mine\n---\nuser content")

    def put_skill(name, body):
        d = os.path.join(skills_src, name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "SKILL.md"), "w") as f:
            f.write(body)

    put_skill("bundled-a", "v1")
    n = sync_skills(src=skills_src, dest=skills_dest)
    check("sync_skills copies bundled skills into the pi config",
          n == 1 and open(os.path.join(skills_dest, "bundled-a",
                                       "SKILL.md")).read() == "v1")
    check("container-tools skill ships with the repo",
          os.path.exists(os.path.join(ROOT, "skills", "container-tools",
                                      "SKILL.md"))
          and "container-tools" in open(
              os.path.join(ROOT, "skills", "container-tools",
                           "SKILL.md")).read())
    put_skill("bundled-a", "v2")
    put_skill("bundled-b", "new")
    sync_skills(src=skills_src, dest=skills_dest)
    check("sync_skills updates changed skills on next start",
          open(os.path.join(skills_dest, "bundled-a",
                            "SKILL.md")).read() == "v2"
          and os.path.exists(os.path.join(skills_dest, "bundled-b",
                                          "SKILL.md")))
    check("sync_skills leaves user skills untouched",
          open(os.path.join(user_skill, "SKILL.md")).read()
          .endswith("user content"))
    shutil.rmtree(skills_src, ignore_errors=True)
