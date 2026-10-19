# Tasks

A task is one piece of work you want your agent to get better at: a bug to fix, a feature to
add, a report to write. Harness² runs the agent on each task, studies how the runs went, and
refines the agent's harness on that evidence. Good tasks are real work, phrased the way you
would actually ask, and varied enough that a better harness has to be better in general.

- [Layout](#layout)
- [Workspaces](#workspaces)
- [Deliverables](#deliverables)
- [Grading](#grading)
- [Creating and importing tasks](#creating-and-importing-tasks)

## Layout

Each task is a directory under `.harness2/tasks/` (the `paths.tasks` setting). Its name is the
task id: lowercase letters, digits, `.`, `_` and `-`.

```
.harness2/tasks/
  fix-login-redirect/
    task.md          the instruction the agent receives (required)
    task.toml        options (optional)
    check/           files only the grader sees (optional)
    rubric.md        criteria for an LLM grader (optional)
  summarize-incidents/
    task.md
    task.toml
    files/           input files copied into the workspace (optional)
```

`task.md` is given to the agent as-is. Write it as a request, not as a test: the agent, the
improver and the selector all read it, and none of them ever sees `check/` or `rubric.md`.

`task.toml` keys, all optional:

| Key | Default | Meaning |
|---|---|---|
| `domain` | `"default"` | Groups related tasks. Lessons and adopted harnesses carry over only between tasks of the same domain, and domains can run in parallel. |
| `split` | none | `"train"` or `"test"`, for `harness2 run --split`. A task without one is only in `all`. |
| `workspace` | `"files"` if `files/` exists, else `"project"` | Where the agent works. See [Workspaces](#workspaces). |
| `ref` | `"HEAD"` | Project workspaces only: the git revision the agent starts from. |
| `deliverable` | `"diff"` for project workspaces, `"files"` otherwise | What counts as the result. See [Deliverables](#deliverables). |
| `check` | none | A shell command that grades a result. See [Grading](#grading). |
| `timeout` | `executor.timeout` | Seconds one run of this task may take. |

## Workspaces

Every run gets a fresh workspace in a temporary directory outside your project, so runs
never touch your working tree and never see each other.

**`project`**: a clean `git worktree` of your repository at `ref`. The project must be a git
repository, and uncommitted changes are not included: commit what the task should start
from. The `.harness2/` directory and the project's own harness files are removed from the
copy, and the harness being tried is installed in their place.

**`files`**: an empty directory seeded with the contents of the task's `files/`, plus an
empty `output/` directory. The agent is asked to save its deliverables in `output/`. Use
this for work that is not a change to the repository: documents, analyses, data.

## Deliverables

| `deliverable` | The result is | Counts as delivered when |
|---|---|---|
| `diff` | the change the agent made to the repository, as a patch | the patch is not empty |
| `files` | the files in `output/` | `output/` holds at least one file |
| `message` | the agent's final message | the message is not empty |

The agent's final message is always kept alongside the result. Changes to the harness files
themselves are left out of the patch.

## Grading

Refining the harness does not need grades: candidates are compared by an independent
selector that reads their results, never a score. Grades are for you, to measure what a run
achieved. `harness2 judge` grades the result each task finally adopted, and `harness2 report`
summarises the grades.

**`check`**: a shell command run in a fresh workspace that holds the result: for project
tasks, a new worktree at `ref` with the patch applied; for files tasks, a copy of `files/`
with the result in `output/`. The contents of `check/` are copied in first, so hidden tests
can live there. The command passes when it exits with status 0. For partial credit, print a
JSON object as the last line of output:

```json
{"n_passed": 7, "n_criteria": 9}
```

`HARNESS2_TASK_DIR` and `HARNESS2_RUN_DIR` are set for the command.

```toml
# task.toml
check = "cp check/test_redirect.py tests/ && python -m pytest -q tests/test_redirect.py"
```

**`rubric.md`**: criteria an LLM judge applies to the result, one checkable criterion per
line works best. The judge uses the `[judge]` settings. It reports how many criteria the
result meets.

A task with neither is still refined; it just has no grade.

## Creating and importing tasks

```bash
harness2 task new fix-login-redirect                 # a project task
harness2 task new summarize-incidents --files        # a files task, with files/
harness2 task new add-pagination --domain api --check "python -m pytest -q"
harness2 task ls
```

To bring in many tasks at once, import a JSON Lines or CSV file. `id` and `prompt` are
required; the other fields are the `task.toml` keys above.

```bash
harness2 task import tasks.jsonl
```

```json
{"id": "fix-login-redirect", "prompt": "Users who log in from /settings land on / ...", "domain": "auth", "check": "python -m pytest -q tests/test_login.py"}
```
