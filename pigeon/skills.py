"""Bundled skills: at startup they are copied into pi's user skills dir
($PI_CODING_AGENT_DIR/skills) so they are part of the persistent agent
configuration; bundled skills are updated on every start, user skills
(other folders) are left untouched."""

import os
import shutil

from .config import PI_CONFIG_DIR, SKILLS_SRC


def sync_skills(src=SKILLS_SRC, dest=None):
    """Copy bundled skill folders into dest. Returns number synced."""
    if dest is None:
        dest = os.path.join(PI_CONFIG_DIR, "skills")
    if not os.path.isdir(src):
        return 0
    os.makedirs(dest, exist_ok=True)
    n = 0
    for name in sorted(os.listdir(src)):
        s = os.path.join(src, name)
        d = os.path.join(dest, name)
        if not os.path.isdir(s) or \
                not os.path.exists(os.path.join(s, "SKILL.md")):
            continue
        if os.path.isdir(d):
            shutil.rmtree(d)
        shutil.copytree(s, d)
        n += 1
    return n
