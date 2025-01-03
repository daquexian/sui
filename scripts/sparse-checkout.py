#!/usr/bin/env python3

import os
import subprocess
import sys

try:
    import toml
except ImportError:
    print("This script requires the 'toml' Python package. Install via: pip install toml")
    sys.exit(1)


def read_sparse_config(sparse_file=".sparse"):
    """
    Read the .sparse file and return a list of crates (paths) that should be included.
    """
    if os.path.isfile(sparse_file):
        crates = []
        with open(sparse_file, "r") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    crates.append(line)
        # throw error if no crates are found
        if not crates:
            print(f"No crates found in {sparse_file}. Exiting.")
            sys.exit(1)
        return crates
    else:
        return None


def update_git_sparse_checkout(crates_to_checkout):
    """
    Initialize or update the git sparse-checkout to include only the given crates.
    """

    # You can add any default directories you always want checked out here
    default_directories = ["scripts", ".cargo", ".changeset", ".config", ".github"]

    # 1) Initialize sparse checkout (if not already).
    subprocess.check_call(["git", "sparse-checkout", "init", "--cone"])

    # 2) Set the paths we actually want to check out.
    cmd = ["git", "sparse-checkout", "set"] + crates_to_checkout + default_directories
    subprocess.check_call(cmd)

    # 3) run git checkout to refresh the sparse checkout
    subprocess.check_call(["git", "checkout"])

def modify_cargo_toml(crates_to_checkout, cargo_toml_path="Cargo.toml"):
    """
    Remove crates not in crates_to_checkout from Cargo.toml [workspace.members].
    Then, for each missing crate:
      - Locate a matching dependency in [workspace.dependencies] that has path=<crate_path>.
      - Convert it from path-based to git-based, preserving the original dependency name (TOML key).
      - If no matching dependency is found, create a new entry with a fallback name.
    """
    # Load the Cargo.toml
    if not os.path.isfile(cargo_toml_path):
        print(f"Could not find {cargo_toml_path}. Exiting.")
        sys.exit(1)

    # Use git to get the contents of Cargo.toml at the current commit
    try:
        cargo_toml_content = subprocess.check_output(["git", "show", f"HEAD:{cargo_toml_path}"]).decode()
        cargo_data = toml.loads(cargo_toml_content)
    except subprocess.CalledProcessError:
        print(f"Could not retrieve {cargo_toml_path} from git. Exiting.")
        sys.exit(1)

    # Make sure we have workspace.members in the top-level Cargo.toml
    workspace = cargo_data.setdefault("workspace", {})
    members = workspace.get("members", [])
    excluded = workspace.get("exclude", [])

    all_directories = members + excluded

    # Determine which crates are missing from .sparse
    missing_crates = [m for m in all_directories if m not in crates_to_checkout]
    kept_crates = [m for m in members if m in crates_to_checkout]

    # Update workspace.members
    cargo_data["workspace"]["members"] = kept_crates

    # Get the merge base of the current commit and origin/main
    commit_sha = subprocess.check_output(["git", "merge-base", "HEAD", "origin/main"]).decode().strip()

    # Get the git remote URL (assuming 'origin' is the correct remote)
    try:
        repo_url = subprocess.check_output(["git", "remote", "get-url", "origin"]).decode().strip()
        # Convert SSH URL (git@...) to HTTPS if desired
        if repo_url.startswith("git@"):
            # Example conversion: git@github.com:User/Repo.git -> https://github.com/User/Repo.git
            repo_url = repo_url.replace(":", "/").replace("git@", "https://")
    except subprocess.CalledProcessError:
        # If there's no 'origin', handle as you see fit
        repo_url = "https://unknown-repo-url"

    # Ensure [workspace.dependencies] is a dict
    workspace_deps = cargo_data["workspace"].setdefault("dependencies", {})

    # For each missing crate, we want to:
    # 1) Find a dependency in [workspace.dependencies] whose 'path' == crate_path.
    # 2) Replace that 'path' dep with a 'git' + 'rev' dep, preserving the key.
    # 3) If none is found, create a new dependency entry with a fallback name.
    for crate_path in missing_crates:
        matched_dep = False

        for dep_name, dep_spec in workspace_deps.items():
            # If dep_spec is not a dict, skip
            if not isinstance(dep_spec, dict):
                continue

            # If the 'path' matches, update it
            if dep_spec.get("path") == crate_path:
                del dep_spec["path"]
                dep_spec["git"] = repo_url
                dep_spec["rev"] = commit_sha
                matched_dep = True
                break

        if not matched_dep:
            # No existing dependency matched this path,
            # so create one with a fallback name derived from the directory.
            crate_name = os.path.basename(os.path.normpath(crate_path))
            workspace_deps[crate_name] = {
                "git": repo_url,
                "rev": commit_sha,
            }

    # Write updated Cargo.toml back
    with open(cargo_toml_path, "w") as f:
        toml.dump(cargo_data, f)

    print("Successfully updated Cargo.toml")

def get_ignored_files():
    ignored_files = subprocess.check_output(["git", "ls-files", "-v"]).decode().split("\n")
    ignored_files = [line.split(" ")[1] for line in ignored_files if line.startswith("h")]
    return ignored_files

def ignore_cargo_changes():
    """
    Ignore changes to Cargo.toml and Cargo.lock with the --assume-unchanged flag.
    """

    ignored_files = get_ignored_files()

    # ignored files should include only Cargo.toml and Cargo.lock
    if "Cargo.toml" not in ignored_files:
        print("Ignoring changes to Cargo.toml");
        subprocess.check_call(["git", "update-index", "--assume-unchanged", "Cargo.toml"])
    else:
        ignored_files.remove("Cargo.toml")

    if "Cargo.lock" not in ignored_files:
        print("Ignoring changes to Cargo.lock");
        subprocess.check_call(["git", "update-index", "--assume-unchanged", "Cargo.lock"])
    else:
        ignored_files.remove("Cargo.lock")

    # un-ignore any remaining files
    for file in ignored_files:
        print(f"Un-ignoring {file}")
        subprocess.check_call(["git", "update-index", "--no-assume-unchanged", file])

def create_sparse_checkout_worktree():
    # if .sparse is not found, offer to create a new sparse worktree
    print("No crates found in .sparse (or file not present).")
    print("Would you like to create a new sparse worktree? (Y/n)")
    choice = input().lower()
    if choice == "y" or choice == "":
        # move to git repo root
        os.chdir(subprocess.check_output(["git", "rev-parse", "--show-toplevel"]).decode().strip())
        # get basename of current directory
        dir = os.path.basename(os.getcwd())
        sparse_dir = f"../{dir}-sparse"

        # ask if they would like to use this name or a different one
        print(f"Would you like to use the directory name '{sparse_dir}' for the sparse worktree? (Y/n)")
        choice = input().lower()
        if choice == "n":
            print("Enter the name for the sparse worktree:")
            sparse_dir = input()
            # add ../ if not already present
            if not sparse_dir.startswith("../"):
                sparse_dir = f"../{sparse_dir}"

        print(f"Creating a new sparse worktree at {sparse_dir}")
        subprocess.check_call(["git", "worktree", "add", "--no-checkout", sparse_dir, "main"])

        # move to the sparse worktree
        os.chdir(sparse_dir)

        # now launch $EDITOR to configure the .sparse file. The default contents of .sparse
        # are `crates/sui-core`. First, write the defaults
        with open(".sparse", "w") as f:
            f.write("# Directories to include in the sparse checkout\n")
            f.write("crates/sui-core\n")
        # now launch $EDITOR
        subprocess.check_call([os.getenv("EDITOR", "vi"), ".sparse"])
    else:
        print("Exiting.")
        sys.exit(0)
    crates_to_checkout = read_sparse_config(".sparse")
    return crates_to_checkout

def reset_index():
    ignored_files = get_ignored_files()

    # check that Cargo.toml and Cargo.lock are ignored
    if "Cargo.toml" not in ignored_files or "Cargo.lock" not in ignored_files:
        print("Cargo.toml and/or Cargo.lock are not ignored. Reset them manually or check in your changes")
        sys.exit(1)

    subprocess.check_call(["git", "checkout", "Cargo.toml", "Cargo.lock"])

def main():
    # if given the `reset` command, reset changes to Cargo.lock and Cargo.toml
    if len(sys.argv) > 1 and sys.argv[1] == "reset":
        reset_index()
        sys.exit(0)

    # 1. Read the crates to include from .sparse
    crates_to_checkout = read_sparse_config(".sparse")
    if crates_to_checkout is None:
        crates_to_checkout = create_sparse_checkout_worktree()
        assert crates_to_checkout is not None

    # 2. Update git sparse checkout
    update_git_sparse_checkout(crates_to_checkout)

    # 3. Ignore changes to Cargo.toml and Cargo.lock
    ignore_cargo_changes()

    # 4. Modify Cargo.toml
    modify_cargo_toml(crates_to_checkout, "Cargo.toml")


if __name__ == "__main__":
    main()
