# How it works

Harness² improves an agent without touching the model. It changes the **harness**: the
files the agent reads and runs around every task. For Claude Code those are `CLAUDE.md`,
skills, subagents, helper scripts and hooks; for Codex, `AGENTS.md`, skills, scripts and
hooks. The method is described in the paper
[Harness²: Recursive Agent Harnessing for an Open World](https://arxiv.org/abs/2610.14224).
This page explains what a run does, what it costs, and what you get back, including the
parts Harness² CLI adds to the paper's method: one final harness per run, `eval` and
`apply`.

- [The harness components](#the-harness-components)
- [One task: propose, probe, compose](#one-task-propose-probe-compose)
- [Scaling with k](#scaling-with-k)
- [Picking each task's harness](#picking-each-tasks-harness)
- [Carrying lessons forward](#carrying-lessons-forward)
- [One harness for the project](#one-harness-for-the-project)
- [What a run gives you](#what-a-run-gives-you)

## The harness components

Each component has a different cost and reach, and the improver is told to use the cheapest
one that fixes what it saw:

| Component | Claude Code | Codex | In context |
|---|---|---|---|
| Always-on instructions | `CLAUDE.md` | `AGENTS.md` | every turn, so every word costs |
| Skills | `.claude/skills/<name>/SKILL.md` | `.agents/skills/<name>/SKILL.md` | only when invoked |
| Subagents | `.claude/agents/<name>.md` | — | in their own context |
| Helper scripts | `.claude/scripts/*.py` | `.agents/scripts/*.py` | only when run |
| Hooks | `hooks` in `.claude/settings.json` | `.codex/hooks.json` | inside the tool loop, cannot be skipped |

With the `command` executor the harness is whatever files `executor.editable` matches.

Harness² edits only these files. Anything else in `.claude/` or in a skill directory, such as
reference documents, is left alone.

## One task: propose, probe, compose

For each task, a run:

1. **Executes** the agent with the current harness, `k` times, to see how it behaves today.
2. **Proposes** edits. Two improvers read those trajectories and results independently. One
   works on *how the agent works*: procedure, tool use, checking its own result. The other
   works on *what a practitioner knows* about this kind of task. Each writes a candidate
   harness.
3. **Probes** every candidate by running the agent with it on the same task.
4. **Composes** a refined harness from the contrast between all those runs: which edits
   changed behaviour, and how. The refined harness is run too.

No step looks at a grade. The improvers see trajectories and results, and decide from the
differences between runs what helped. This is what lets Harness² work on tasks that have no
test at all.

If the evidence does not support a change, an improver can keep the harness as it is. An
edit that breaks the harness's rules (a skill without a description, a script that is
referenced but missing, malformed hooks) is sent back once with the problems listed, and
otherwise dropped.

## Scaling with k

`k` (`--k`, default 1) is how much effort each task gets. There are two ways to spend it:

- **parallel** (`--mode parallel`, the default): `k` "how it works" and `k` "what it knows"
  candidates are proposed from the same evidence, and `k` composed finals are built from all
  of them. Wider exploration.
- **sequential** (`--mode sequential`): `k` rounds of propose–probe–compose, each starting
  from the previous round's result and seeing all earlier evidence. Deeper refinement.

Per task and mode, a run makes **4k agent runs** (including the `k` runs of the current
harness, which both modes share) and **3k improver sessions**, plus one reflector session
that writes the task's lesson and, when `k` is more than 1, one selector session that picks
the task's harness. Building the final harness adds one session per run and mode.
`harness2 run --dry-run` prints the exact counts for your tasks before anything is spent,
and `--budget-usd` / `--max-calls` stop a run at a limit.

## Picking each task's harness

With `k` = 1 there is one composed harness per task. With more, a **selector** compares the
composed harnesses' results blind: it sees the task and each result under an anonymous id,
never which harness produced it or how that harness was built, and picks the best. That pick
is the task's **adopted** harness.

You can make the pick yourself with `harness2 run --selector human`: the run stops at each
selection and shows you the anonymous candidates in the terminal.

## Carrying lessons forward

After a task adopts a harness, a **reflector** looks at which edits made a difference and
writes a short lesson. The adopted harness and its lesson go into a **library** for the
task's domain. Later tasks in the same domain show the library to their improvers, so useful
components get reused and verified lessons build up. This is the second level of recursion:
the harness that improves harnesses gets better as tasks go by.

Tasks in a domain run one after another so the library can grow; separate domains run in
parallel (`--workers`). Each run has its own library, so experiments never mix.

## One harness for the project

In the paper, every task ends with its own adopted harness: the method adapts the agent to
the task in front of it, and the adopted harnesses are kept for scoring and as examples for
later tasks. A project needs one harness that it can keep. So when all tasks are done,
`harness2 run` adds one step of its own: an improver session reads the starting harness,
every task's request, the harness adopted for it and the lesson recorded, and composes **one
final harness** for the project's next tasks. It keeps what helped on several tasks or what
a lesson supports as general, and drops facts tied to a single task. The result is checked
against the same rules as every other proposal.

That final harness is what you work with:

- `harness2 diff` shows how it differs from the harness you started with;
- `harness2 eval` runs the agent with both on the tasks that have a check or rubric and
  compares the pass rates, the evidence for whether it actually helps; run it on `test`
  tasks the run did not use for the fairest comparison;
- `harness2 apply` writes it into the project, applying only what the run changed, so edits
  you made meanwhile are kept, and touching nothing but the harness files.

## What a run gives you

Everything is stored under `.harness2/runs/<name>/`:

- `run.json`: the settings and task list the run was started with.
- `events.jsonl`: one line per agent run, session and decision, with costs.
- `adapt/`: per task, every candidate harness, the improvers' notes, the selection and the
  lesson.
- `rollouts/`: per agent run, the trajectory, the result and the final message.
- `library/`: the adopted harnesses and lessons per domain.
- `consolidated/`: the final harness and the improver's notes on why each change is there.

Use `harness2 status` while it runs, `harness2 diff`, `harness2 eval` and `harness2 apply` on
the final harness, and `harness2 judge` / `harness2 report` to grade each task's adopted
result. Running the same `harness2 run` command again resumes where it stopped, reusing every
finished step.
