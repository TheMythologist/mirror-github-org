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


def check_rate_limiting(rl):
    remaining, total = rl._requester.rate_limiting

    if remaining < RATE_BUFFER:
        reset_time = rl._requester.rate_limiting_resettime
        reset_time_human = datetime.datetime.fromtimestamp(
            int(reset_time)
        ) + datetime.timedelta(seconds=EXTRA_WAIT)

        print(
            "\nWAITING: Remaining rate limit is %s of %s. Waiting %s mins for reset at %s before continuing.\n"
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
        print(f"{src_repo.name} repository already exists, updating")
    except UnknownObjectException as e:
        if e.status != 404:
            raise
        print(f"{src_repo.name} repository does not exist, creating")
        dst_repo = dst_org.create_repo(src_repo.name, private=True)
        # TODO: Disable github actions

    old_repo_url = (
        f"https://{token}:x-oauth-basic@github.com/{src_org_str}/{src_repo.name}"
    )
    new_repo_url = (
        f"https://{push_token}:x-oauth-basic@github.com/{dst_org_str}/{dst_repo.name}"
    )

    print(f"Cloning {src_repo.name}")

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
    new_remote.push(force=True).raise_if_error()
    rmtree(src_repo.name)


if __name__ == "__main__":
    p = {}
    for param in ("GITHUB_TOKEN", "SRC_ORG", "DST_ORG"):
        p[param] = os.getenv(param)
        if not p[param]:
            print(f"No {param} supplied in env")
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
                print(f"Mirror task failed for {repo_name}: {e}")
                failed.append(repo_name)

    if failed:
        print(f"\n{len(failed)} mirror task(s) failed: {', '.join(failed)}")
        sys.exit(1)
