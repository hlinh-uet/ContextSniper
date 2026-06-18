"""Local SWE-bench harness tweaks for offline-ish validation.

When SWEBENCH_LOCAL_REPO_URL_<repo> is set, rewrite the generated repo
setup command to clone from that URL instead of GitHub. This keeps the
SWE-bench Docker harness intact while avoiding flaky external clones during
parallel experiments.
"""
import os


def _env_key(repo):
    return "SWEBENCH_LOCAL_REPO_URL_" + "".join(ch if ch.isalnum() else "_" for ch in repo)


def _patch_python_repo_script():
    try:
        import swebench.harness.test_spec.python as py_mod
        import swebench.harness.test_spec.create_scripts as create_mod
    except Exception:
        return

    original = py_mod.make_repo_script_list_py

    def wrapped(specs, repo, repo_directory, base_commit, env_name):
        commands = original(specs, repo, repo_directory, base_commit, env_name)
        local_url = os.environ.get(_env_key(repo))
        if not local_url:
            return commands
        for i, cmd in enumerate(commands):
            if cmd.startswith("git clone ") and f"https://github.com/{repo}" in cmd:
                commands[i] = cmd.replace(f"https://github.com/{repo}", local_url)
                commands[i] = commands[i].replace(" --single-branch", "")
                break
        return commands

    py_mod.make_repo_script_list_py = wrapped
    create_mod.make_repo_script_list_py = wrapped


_patch_python_repo_script()
