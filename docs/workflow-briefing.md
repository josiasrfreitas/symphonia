# Importable Development Workflow Briefing

## Purpose

This project will package a repeatable development workflow that can be brought into a new codebase at project start. The package should give agents and humans the same operating model: how to orient, how to preserve context, how to implement safely, and how to close work without losing the decisions that shaped it.

The workflow combines guardrails, planning practices, context files, review loops, and agent coordination patterns. A new project should be able to import the workflow and receive a clear default path for feature work, bug fixes, refactors, and pull requests.

## Current Scope

The first pass defines three major phases.

1. **Wayfinding**
   Establish the source of truth, classify the work, and decide how much planning the task needs. Simple fixes can move directly into a short plan. Ambiguous or complex work needs a stronger specification before anyone writes code.

2. **Context Management**
   Create and maintain the working context for the task. This includes the issue or task record, the implementation plan, project notes, branch or worktree setup, baseline checks, and status updates that let another agent resume the work.

3. **Implementation**
   Use an orchestrator to break the plan into coherent tasks. The orchestrator coordinates the work and review flow. Subagents handle implementation, testing, debugging, and focused fixes. Independent tasks can run in parallel. Dependent tasks run in sequence.

## Operating Principles

- Keep one source of truth for the task.
- Write down decisions before they become hidden assumptions.
- Use isolated workspaces or branches for implementation.
- Run a clean baseline before changing behavior.
- Make the orchestrator responsible for coordination, delegation, and review.
- Give subagents narrow tasks with clear inputs and outputs.
- Use tests to drive risky behavior changes.
- Treat review findings as claims that need evidence.
- Close the loop with verification notes and context cleanup.

## Mermaid Workflow

```mermaid
flowchart TD
    START([New project or task]) --> W1

    subgraph W["Phase 1: Wayfinding"]
        W1[Identify request type] --> W2{Source of truth exists?}
        W2 -->|No| W3[Create issue or task record]
        W2 -->|Yes| W4[Read existing source]
        W3 --> W5
        W4 --> W5{Complexity level}
        W5 -->|Clear bug or small refactor| W6[Create direct plan]
        W5 -->|Ambiguous or high impact| W7[Run discovery and write spec]
        W5 -->|Plan already exists| W8[Review plan]
        W6 --> W9[Confirm success criteria]
        W7 --> W9
        W8 --> W9
    end

    W9 --> C1

    subgraph C["Phase 2: Context Management"]
        C1[Create project context bundle] --> C2[Add index, plan, notes, and references]
        C2 --> C3[Create isolated branch or workspace]
        C3 --> C4[Install dependencies]
        C4 --> C5[Run baseline checks]
        C5 -->|Fails| C6[Record blocker and diagnose baseline]
        C6 --> C5
        C5 -->|Passes| C7[Post start status with branch and plan]
    end

    C7 --> I1

    subgraph I["Phase 3: Implementation"]
        I1[Orchestrator reviews plan] --> I2{Tasks independent?}
        I2 -->|Yes| I3[Launch subagents in parallel]
        I2 -->|No| I4[Run subagents in sequence]
        I3 --> I5[Each subagent implements scoped task]
        I4 --> I5
        I5 --> I6[Run focused tests]
        I6 -->|Fail| I7[Diagnose root cause and fix]
        I7 --> I5
        I6 -->|Pass| I8[High-capability review for spec fit]
        I8 --> I9[High-capability review for code quality]
        I9 --> I10{Review findings?}
        I10 -->|Yes| I11[Delegate corrections]
        I11 --> I6
        I10 -->|No| I12[Run final verification]
    end

    I12 --> CLOSE([Ready for PR, handoff, or project import])
```

## Next Decisions

- Define the file structure that a project imports.
- Decide which guardrails belong in reusable Markdown, scripts, skills, or templates.
- Specify the minimum context bundle needed for another agent to resume work.
- Define how the orchestrator records subagent assignments and outcomes.
- Choose the first project to test the workflow against.
