import sys
import os
import time
import datetime
import concurrent.futures

from git import Repo, rmtree
from github import Auth, Github
from github.GithubException import UnknownObjectException

RATE_BUFFER = 100
EXTRA_WAIT = 60

RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
RESET = "\033[0m"


def check_rate_limiting(rl):
    remaining, total = rl._requester.rate_limiting

    if remaining < RATE_BUFFER:
        reset_time = rl._requester.rate_limiting_resettime
        reset_time_human = datetime.datetime.fromtimestamp(
            int(reset_time)
        ) + datetime.timedelta(seconds=EXTRA_WAIT)

        print(
            f"\n{YELLOW}WAITING: Remaining rate limit is %s of %s. Waiting %s mins for reset at %s before continuing.{RESET}\n"
            % (remaining, total, int((reset_time - time.time()) / 60), reset_time_human)
        )

        while time.time() <= (reset_time + EXTRA_WAIT):
            time.sleep(60)
            print(".", end="")

        print("\n")


def mirror(
    token: str,
    src_org_str: str,
    dst_org_str: str,
    src_repo_str: str,
    push_token: str | None,
):
    if push_token is None:
        push_token = token
    g = Github(auth=Auth.Token(token))

    src_org = g.get_organization(src_org_str)
    src_repo = src_org.get_repo(src_repo_str)
    dst_org = Github(auth=Auth.Token(push_token)).get_organization(dst_org_str)

    check_rate_limiting(src_repo)

    # Create private repository
    try:
        dst_repo = dst_org.get_repo(src_repo.name)
        print(f"{CYAN}{src_repo.name} repository already exists, updating{RESET}")
    except UnknownObjectException as e:
        if e.status != 404:
            raise
        print(f"{CYAN}{src_repo.name} repository does not exist, creating{RESET}")
        dst_repo = dst_org.create_repo(src_repo.name, private=True)
        # TODO: Disable github actions

    old_repo_url = (
        f"https://{token}:x-oauth-basic@github.com/{src_org_str}/{src_repo.name}"
    )
    new_repo_url = (
        f"https://{push_token}:x-oauth-basic@github.com/{dst_org_str}/{dst_repo.name}"
    )

    print(f"{CYAN}Cloning {src_repo.name}{RESET}")

    repo = Repo.clone_from(
        old_repo_url,
        src_repo.name,
        single_branch=True,
        env={"GIT_LFS_SKIP_SMUDGE": "1"},
    )

    new_remote = repo.create_remote(
        "new_remote",
        new_repo_url,
    )

    branch_name = repo.active_branch.name
    try:
        new_remote.fetch()
        remote_commit = repo.commit(f"new_remote/{branch_name}")
    except Exception:
        remote_commit = None

    if remote_commit is not None and not repo.is_ancestor(
        remote_commit, repo.head.commit
    ):
        timestamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
        backup_branch = f"{branch_name}-diverged-{timestamp}"
        print(
            f"{YELLOW}Divergence detected for {src_repo.name}/{branch_name}; "
            f"preserving destination history at {backup_branch}{RESET}"
        )
        new_remote.push(
            refspec=f"{remote_commit.hexsha}:refs/heads/{backup_branch}"
        ).raise_if_error()

    new_remote.push(force_with_lease=True).raise_if_error()
    rmtree(src_repo.name)
    print(f"{GREEN}Successfully mirrored {src_repo.name}{RESET}")


if __name__ == "__main__":
    p = {}
    for param in ("GITHUB_TOKEN", "SRC_ORG", "DST_ORG"):
        p[param] = os.getenv(param)
        if not p[param]:
            print(f"{RED}No {param} supplied in env{RESET}")
            sys.exit(1)

    push_token = os.getenv("PUSH_TOKEN")
    if push_token == "None":
        push_token = None

    skip_repos = {
        name.strip()
        for name in os.getenv("SKIP_REPOS", "").split(",")
        if name.strip()
    }

    with concurrent.futures.ThreadPoolExecutor() as pool:
        g = Github(auth=Auth.Token(p["GITHUB_TOKEN"]))
        src_org = g.get_organization(p["SRC_ORG"])

        futures = {
            pool.submit(
                mirror,
                p["GITHUB_TOKEN"],
                p["SRC_ORG"],
                p["DST_ORG"],
                src_repo.name,
                push_token,
            ): src_repo.name
            for src_repo in src_org.get_repos("all", sort="pushed", direction="desc")
            if src_repo.name not in skip_repos
        }

        failed = []
        for future in concurrent.futures.as_completed(futures):
            repo_name = futures[future]
            try:
                future.result()
            except Exception as e:
                print(f"{RED}Mirror task failed for {repo_name}: {e}{RESET}")
                failed.append(repo_name)

    if failed:
        print(f"\n{RED}{len(failed)} mirror task(s) failed: {', '.join(failed)}{RESET}")
        sys.exit(1)
