# Decision Indexing: State of the Art

Research date: 2026-08-05. Primary sources cited inline.

## TLDR

No one has solved this. There is no established convention that maps an architecture decision to an area of the codebase — not in ADRs, not in arc42, not in C4, not in any RFC process. Every ADR index generator in existence emits the same thing: a flat, number-ordered bullet list of `link + title`, with no status, no tags, no code scope. The only mature path-to-metadata mechanism in the industry is CODEOWNERS, and it carries owners, not decisions. Meanwhile the agent-context world has independently converged on exactly the two-tier shape we want — a small always-loaded index of one-line descriptions plus stable addresses, bodies one hop away — with real enforced budgets (200 lines / 25KB; ~1k tokens). The design we should copy is PEP's: the index is a pure derived artifact regenerated from per-record headers, so it structurally cannot drift. What we would be building is a new convention, not an adopted one.

---

## 1. ADRs: Nygard, MADR, and the tooling ecosystem

### 1.1 Nygard's original format (2011)

[Documenting Architecture Decisions](https://www.cognitect.com/blog/2011/11/15/documenting-architecture-decisions) defines exactly five fields: **Title, Context, Decision, Status, Consequences**.

On storage: "We will keep ADRs in the project repository under `doc/arch/adr-NNN.md`." On numbering: "ADRs will be numbered sequentially and monotonically. Numbers will not be reused." On supersession: "If a decision is reversed, we will keep the old one around, but mark it as superseded."

**There is no index in the original.** No table of contents, no tags, no cross-reference syntax. The only relationship Nygard names is prose-level and implicit: "The consequences of one ADR are very likely to become the context for subsequent ADRs." Sequential numbers and a status word are the entire linking story. Everything else in the ecosystem is later invention.

Martin Fowler's [bliki entry](https://martinfowler.com/bliki/ArchitectureDecisionRecord.html) adds the discoverability argument — records live in the repo "so they are easily available to those working on the code base," each numbered "so that they are easy to read in a directory listing" — and names the one scale limit: "Storing them in a product repository won't work for ADRs that cover a broader ecosystem than a single code base." It defines no index mechanism either.

### 1.2 MADR

[MADR](https://adr.github.io/madr/) ([repo](https://github.com/adr/madr)) adds YAML frontmatter:

```yaml
status: "{proposed | rejected | accepted | deprecated | … | superseded by ADR-0123}"
date: {YYYY-MM-DD when the decision was last updated}
decision-makers: {list everyone involved in the decision}
consulted: {list everyone whose opinions are sought ...}
informed: {list everyone who is kept up-to-date on progress ...}
```

Three things matter here, and two of them are absences.

**MADR has no tags field.** Not in frontmatter, not in the body. `docs/decisions/0010-support-categories.md` explicitly rejected filename tagging ("Bad, because as bad as TagSpaces, which stores the tags in the filenames in brackets"). The substitute is **categories as subfolders** (`decisions/backend/`, `decisions/ui/`) — with the accepted consequence that "numbers of ADRs are no longer unique throughout the repository, but locally within a category only."

**MADR has no link-to-code mechanism.** The nearest thing is a prose instruction in the Context section: "Define the scope explicitly by referencing architectural components or connectors." Free text, not a field.

**MADR defines no index file — it delegates.** `0004-write-own-toc-tool.md` states the problem plainly: "ADRs have to be indexed somehow. E.g., for offering a website showing all ADRs." The chosen option was to write `adr-log`, "because we want to have the format `ADR-0001 - Title` in the TOC. `adr-tools` offers `title` only." That is the depth of the state of the art: a whole decision record was written to settle whether the index line includes the ID.

ADR-to-ADR linking was settled in `0009`: put an ordinary markdown link in the "More Information" section, with the noted downside "Bad, because parsing gets harder."

### 1.3 What each tool's index artifact actually contains

| Tool | Index artifact | Fields carried |
|---|---|---|
| [adr-tools](https://github.com/npryce/adr-tools) `adr generate toc` | stdout, not a file | `* [$title]($link)` — title + filename only |
| [adr-tools](https://github.com/npryce/adr-tools) `adr generate graph` | Graphviz image | nodes = titles, edges = sequence + Status-section links |
| [adr-log](https://github.com/adr/adr-log) | injected into `index.md` between `<!-- adrlog -->` markers | `* [ADR-0000](0000-example-1.md) - Example 1` |
| [log4brains](https://github.com/thomvaill/log4brains) | static site: chronological timeline + full-text search | metadata guessed from raw text and git logs; per-package scoping |
| [adr-viewer](https://github.com/mrwilson/adr-viewer) | single static `index.html` | rendered listing |
| [adr-manager](https://github.com/adr/adr-manager) | **none** — it is an editor | n/a |

Read that table plainly: **every index generator in the ADR ecosystem produces a flat, sequentially ordered bullet list of link + title.** Not one indexes by status, component, tag, or supersession chain. A superseded ADR is indistinguishable from a live one in every generated TOC. The only relational view anywhere is `adr generate graph`, and it renders to a picture.

`adr link SOURCE LINK TARGET REVERSE-LINK` (e.g. `adr link 12 Amends 10 "Amended by"`) rewrites both files, injecting a markdown line into each one's `## Status` section. Links live as prose inside a section — there is no link index. `adr new -s 9` does the same for supersession.

adr-tools supports exactly one ADR directory, default `doc/adr`, overridable via a `.adr-dir` file. No multi-directory support.

### 1.4 The ADR org's own guidance

[adr.github.io](https://adr.github.io/) is a link farm, not a specification. Its `/ad-practices/` page disclaims: "The lists on this page point at ADR capturing practices and related advice but do not necessarily endorse all of them."

Concretely: the `NNNN-title-with-dashes.md` convention is **MADR's** (its ADR-0005), not a site-wide standard. The proposed/accepted/deprecated/superseded lifecycle lives in a MADR frontmatter placeholder, not in a normative document. There is no guidance on organizing hundreds of ADRs, no index convention, and no supersession convention beyond the `superseded by ADR-0123` status string.

### 1.5 What is documented to break down at scale

This is the weakest evidence area in the whole survey, and honesty matters more than volume here. Most "ADRs don't scale" content on the open web is SEO listicles with no named team, no numbers, and no date. Excluding those, here is what has real provenance:

**[ThoughtWorks Technology Radar](https://www.thoughtworks.com/radar/techniques/lightweight-architecture-decision-records)** — Trial Nov 2016 and Mar 2017, **Adopt** Nov 2017 and May 2018. Recommends source control over wikis. Note honestly: this is an endorsement, not a critique. It reports no scaling failure.

**[Microsoft Azure Well-Architected Framework](https://learn.microsoft.com/en-us/azure/well-architected/architect-role/architecture-decision-record)** — the only major-vendor doc that names scale directly: "Tracking status makes the current state of each decision clear, **especially as the number of decisions grows**." It mandates append-only: "Don't go back and edit accepted records. If a decision changes, write a new record that supersedes the original and link the two together." And it names the loss mode: "A decision that's made but never recorded will likely be forgotten, leading to repeated debates or later changes that unknowingly contradict the original intent." Note the tension — the append-only mandate *creates* the superseded-chain problem rather than solving it.

**[AWS Prescriptive Guidance](https://docs.aws.amazon.com/prescriptive-guidance/latest/architectural-decision-records/best-practices.html)** — its entire discoverability answer is a manual index: "Store ADRs in a central location… We recommend that you store the ADRs in a central location and **reference them on the main page of your project documentation**." It also names the drift problem without solving it: "The ADR process doesn't solve the issue of non-compliant legacy code."

**[Olaf Zimmermann, "How to create ADRs — and how not to"](https://ozimmer.ch/practices/2023/04/03/ADRCreation.html)** (Zimmermann co-maintains the adr.github.io org) names anti-patterns: Fairy Tale, Sales Pitch, Free Lunch Coupon, Dummy Alternative, Sprint, Tunnel Vision, Maze, Blueprint in Disguise, **Mega-ADR** ("A lot of detailed information about the architecture is stuffed into several multi-page ADRs serving as documentation master (or monster?)"), Novel/Epic, Magic Tricks. Important caveat: **these are all quality-of-one-record anti-patterns.** The most rigorous ADR critique in the literature contains no advice on total count, findability, grouping, or indexing. The corpus-level problem is not addressed.

**Where the evidence runs out:** claims of "we have 200 ADRs and nobody reads them" circulate widely, but no instance could be traced to a named team with a date. Treat that as folklore, not evidence. The strongest citable scaling claim from a primary vendor source is Microsoft's single clause about status tracking mattering more as decisions accumulate.

---

## 2. arc42 and C4: decision-to-component traceability

**arc42 does not define one.** No ID scheme, no link field, no traceability table. It is left entirely to the author.

The [section 5 building block template](https://docs.arc42.org/section-5/) fields are: Purpose/Responsibility, Interface(s), optional Quality/Performance characteristics, optional directory/file location, optional "Fulfilled requirements (if you need traceability to requirements)", optional Open issues. **The only traceability field arc42 offers is to requirements, not to decisions.**

[Section 9](https://docs.arc42.org/section-9/) asks for "Important, expensive, large scale or risky architecture decisions including rationales," motivated so that "Stakeholders of your system should be able to comprehend and retrace your decisions." Its guidance on placement is duplication-avoidance, not linkage — decide whether a decision belongs centrally in section 9 or locally within the white-box template of one building block, and "Avoid redundant texts." The recommended way to associate a decision with a building block is to **move the text into the building block**, not to reference it.

The [arc42 FAQ](https://faq.arc42.org/category_c/) has exactly four section-9 entries, and none is about linking a decision to a building block. The scaling entry, [C-9-4](https://faq.arc42.org/questions/C-9-4/), is arc42's official answer to having many decisions: "Create a blog (RSS-feed) and write a brief entry for your important decisions. Tag those with labels (e.g.: frontend, backend, SAP-interface or similar), so stakeholders can filter them." Free-text tags on a blog. [Tip 9-5](https://docs.arc42.org/tips/9-5/) recommends the Nygard format and adds Starke's criticism that ADRs are "missing *criteria*"; it says nothing about storage or linkage.

**C4 is silent on decisions entirely.** [c4model.com](https://c4model.com/) and its [FAQ](https://c4model.com/faq) contain no mention of decision records. C4 is a diagram and abstraction notation only.

**Structurizr is the entire state of the art for element-scoped decisions**, and its granularity is coarse. From the [DSL language reference](https://docs.structurizr.com/dsl/language): "The `!adrs` keyword can be used to attach Markdown/AsciiDoc ADRs to the parent context (either the workspace, a software system, or a container)."

Three precise facts:

- **Scope is workspace, software system, or container. Components are not listed** — you cannot attach an ADR to a component.
- **Attachment is per-directory, not per-decision.** `!adrs <path> [type|fqn]` points at a *folder*; the default `AdrToolsDecisionImporter` ingests all Markdown files alphabetically (importers: `adrtools`, `madr`, `log4brains`, or a custom class). There is no syntax for "ADR-17 applies to element X."
- The [decisions UI](https://docs.structurizr.com/ui/decisions/) offers a force-directed graph, but it graphs **decision→decision** links, not decision→element links.

So: neither arc42 nor C4 defines decision-to-component traceability. Structurizr's folder-attached-to-a-container is all that exists.

---

## 3. RFC processes: indexing and supersession

| Process | Supersession mechanism | Index generation |
|---|---|---|
| IETF | `Obsoletes` / `Obsoleted by` + `Updates` / `Updated by`, **bidirectional** | XML + XSD, derived from per-document headers (pipeline itself undocumented) |
| Rust RFCs | **manual prose note added to the original file** | mdBook + rfcbot |
| Python PEPs | `Replaces` / `Superseded-By` headers **plus** a `Superseded` status | **fully auto-generated from headers** |
| Oxide RFDs | six-state lifecycle including `committed` | auto-updated CSV + short URLs + chat bot |
| Squarespace | not published | not published — a recurring meeting |

### 3.1 IETF — the strongest supersession model

Supersession is bidirectional header metadata on the documents themselves, mirrored into a machine-readable index. From the [RFC 9110 header](https://www.rfc-editor.org/rfc/rfc9110.txt):

```
Obsoletes: 2818, 7230, 7231, 7232, 7233, 7235,
           7538, 7615, 7694
Updates: 3864
Category: Standards Track
```

The [info page](https://www.rfc-editor.org/info/rfc2616/) carries the computed reverse direction: "This RFC is now obsolete, see RFC 7230, RFC 7231, RFC 7232, RFC 7233, RFC 7234, RFC 7235. This RFC was updated, see RFC 2817, RFC 5785, RFC 6266, RFC 6585."

Two distinct relations, each with a forward and a reverse form — **Obsoletes/Obsoleted by** (full replacement, old document dead) and **Updates/Updated by** (partial amendment, old document still stands). That distinction is worth stealing; ADR practice collapses both into one "superseded" word.

The index is a schema-validated XML document ([rfc-index.xml](https://www.rfc-editor.org/rfc-index.xml), against `rfc-index.xsd`), not merely an HTML page — the HTML rendering exceeds 10MB. Per-entry fields observed directly: `doc-id`, `title`, `author`, `date`, `format`, `page-count`, `keywords`, `abstract`, `draft`, `obsoleted-by`/`obsoletes`, `updated-by`/`updates`, `current-status`, `publication-status`, `stream`, `area`, `wg_acronym`, `doi`, `errata-url`.

Two further details. Info pages show **two status fields** — `current-status` vs `publication-status` — so status can drift after publication independently of the document text. And `<bcp-entry>` blocks with `<is-also>` cross-references (BCP9 → RFC2026, RFC5657, RFC6410, RFC7100, RFC7127, RFC7475, RFC8789, RFC9282) create a **stable name whose membership changes over time** — a grouping layer distinct from supersession.

Honest gap: no primary source describing the index generation pipeline was found. The XSD makes it clearly derived, but do not claim it as sourced.

### 3.2 Rust RFCs

The [README](https://raw.githubusercontent.com/rust-lang/rfcs/master/README.md) line 1 is the index: "Rust RFCs - [RFC Book](https://rust-lang.github.io/rfcs/) - [Active RFC List](https://rfcbot.rs/)". Merged RFCs live in `text/` as `NNNN-title.md`, rendered as an mdBook in number order.

The decision→implementation link is a one-to-one pairing in a different repo: "Every accepted RFC has an associated issue tracking its implementation in the Rust repository."

Supersession is the weakest of the five: "In general, once accepted, RFCs should not be substantially changed. Only very minor changes should be submitted as amendments. More substantial changes should be new RFCs, **with a note added to the original RFC**." No `Superseded-By` header, no status field, no machine-readable link.

**On the "RFCs are not documentation" complaint — be careful.** The README does not say merged RFCs are stale or are not the source of truth. The nearest verbatim statement is: "We strive to write each RFC in a manner that it will reflect the final design of the feature; but the nature of the process means that we cannot expect every merged RFC to actually reflect what the end result will be at the time of the next major release." That is a real admission that a merged RFC may not match shipped reality, but the blunter line that circulates in commentary is **not** in Rust primary material and should not be attributed to the project.

### 3.3 Python PEPs — the index model worth copying

[PEP 1](https://peps.python.org/pep-0001/) statuses: Draft, Accepted, Provisional, Final, Deferred, Rejected, Withdrawn, **Superseded**, Active. Supersession uses a bidirectional header pair — "The newer PEP must have a `Replaces` header containing the number of the PEP that it rendered obsolete", with `Superseded-By` on the old one. Note the design: **Superseded is both a status value and a header field** — the state and the pointer are separate, and both are required. Related headers: `Post-History` (discussion dates, hyperlinked), `Resolution` (URL of the acceptance announcement), `Requires`.

[PEP 0](https://peps.python.org/pep-0000/) is **fully auto-generated, and this is documented**. From the [rendering system docs](https://peps.python.org/docs/rendering_system/): "The generation of the index, PEP 0, happens in three phases. The reStructuredText source file is generated, it is then added to Sphinx, and finally the data is post processed." And: "We first parse the individual PEP files to get the RFC 2822 header, and then parse and validate that metadata." `pep-0000.rst` is created in a Sphinx callback before documents load.

**This is the structural property we want: per-document headers are the only source of truth, the index is regenerated from them on every build, so it cannot drift.**

PEP 0 groups by status (Process/Meta, Other Informational, Provisional, Accepted, Open, Finished, Historical, Deferred, and Rejected/Superseded/Withdrawn), with alternate numerical and topic indices (governance, packaging, release, typing) and a JSON API. Each row is number / title / authors / a two-letter type+status code (`PA`, `IA`, `IF`, `SA`, `SF`, `S`, `I`, `P`) — a compact, high-density encoding worth noting for a token-constrained reader.

### 3.4 Oxide RFDs

[RFD 1](https://rfd.shared.oxide.computer/rfd/0001) ([blog](https://oxide.computer/blog/rfd-1-requests-for-discussion)) defines six states: Prediscussion, Ideation, Discussion, Published, **Committed**, Abandoned. These are lifecycle states, not decision-validity states — with one exception that matters enormously here:

**`committed` means fully implemented and representing current system behavior.** Oxide is the only process surveyed that distinguishes "we agreed to this" (Published) from "this is how the system actually works now" (Committed). That is a real structural answer to the staleness problem Rust explicitly admits to.

Indexing is three deterministic layers: `.helpers/rfd.csv` (automatically updated, every RFD with state and metadata), short URLs (`{num}.rfd.oxide.computer`), and a chat bot doing fuzzy title matching. Numbering is sequential, zero-padded to four digits; a number is claimed by inspecting current git branches. Canonical labels (process, hardware, security) enable category search.

Honest gap: RFD 1 contains **no statement about decisions going stale or requiring periodic review.** The `committed` state implies the concern; nothing states it. Do not attribute a staleness policy to Oxide.

### 3.5 Squarespace

[The Power of "Yes, if"](https://engineering.squarespace.com/blog/2019/the-power-of-yes-if) (Tanya Reilly, Sept 2019) is about review culture, not indexing: a twice-weekly Architecture Review meeting, senior engineers, one hour per major RFC. Reviewers "build up a picture of everything happening in the organization and can notice overlaps or incompatible initiatives." RFCs can never have a "rejected" status, only "not yet."

No published description of how their RFCs are indexed, stored, or superseded. **Squarespace's answer to "does this decision conflict with that one" is a meeting** — that is the notable finding, given that our design goal is a deterministic lookup.

---

## 4. Docs-as-code and living documentation: colocating decisions with code

### 4.1 Living Documentation (Cyrille Martraire)

The core mechanism is "knowledge augmented code": put documentation on the documented thing via **custom annotations on code elements** — classes, and notably **packages via `package-info.java`** — then harvest them at build time to generate glossaries and diagrams. His [workshop repo](https://github.com/cyriux/livingdocumentation-workshop) defines annotations like `@CoreConcept`.

He is explicit in his [InfoQ Q&A](https://www.infoq.com/articles/book-review-living-documentation/) that he ships no universal annotation library — teams "craft their own within their organization."

Honest verdict: he supplies the *pattern* (an annotation on a code element carries design intent; a processor harvests it), and package-level annotation is the closest he gets to "this directory is governed by X". **He does not define an `@ADR` convention**, and no primary source specifies a decision-to-path annotation.

### 4.2 e-ADR — the actual `@ADR` annotation

This is the thing people half-remember as Martraire's. It is from the ADR org: [adr.github.io/e-adr](http://adr.github.io/e-adr/), [repo](https://github.com/adr/e-adr). Two forms:

- `@ADR(1)` — **linked**: the annotation carries only the number; the record lives in `docs/adr/`.
- `@MADR(value=1, title=..., contextAndProblem=..., alternatives=..., chosenAlternative=..., justification=..., relatedDecisions=...)` — **embedded**: the whole record inline.

Attached to Java class declarations in all published examples. This is the only documented code→ADR annotation convention that exists. Adoption is research-project level.

### 4.3 Per-module and colocated ADRs

- **adr-tools**: single configurable directory only (`doc/adr`, or a `.adr-dir` file). No multi-directory support.
- **MADR**: defaults to `docs/decisions`, "does not enforce any repository or directory organization structure", and offers a non-normative community proposal of subdirectories mirroring the architecture, at the cost of globally unique numbering.
- **[log4brains](https://github.com/thomvaill/log4brains)** is the one tool with first-class per-package ADR folders:

```yaml
project:
  name: Foo Bar
  adrFolder: ./docs/adr
  packages:
    - name: backend
      path: ./packages/backend
      adrFolder: ./packages/backend/docs/adr
```

Note precisely what this is: it maps **package → ADR folder**, i.e. scoping by containment. It does not map an individual decision to a set of paths.

Colocation is real practice supported by exactly one mainstream tool, and is nowhere specified as a convention.

### 4.4 Code→ADR references and validators

Comments like `// See ADR-0012` are ad hoc — no specification, style guide, or linter rule was found. **No mainstream tool builds a reverse index from code to ADR.** `mdbook-lint`'s ADR rules only lint the ADR markdown itself.

The single closest match to what we want is [**adrkit**](https://github.com/mbeacom/adrkit), which puts an `affects` field on the record:

```yaml
affects:
  - type: path
    pattern: "apps/web/app/(authed)/**"
  - type: package
    pattern: "next@>=16"
```

with `adr explain <path>` resolving which decisions govern a given file, deterministically, usable in CI. **It is at v0.3.0 with roughly 5 GitHub stars.** Treat it as convergent evidence that the idea is right, not as a convention to adopt.

### 4.5 CODEOWNERS — the strongest prior art for path→metadata

[GitHub's CODEOWNERS](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-code-owners):

- Lives in `.github/`, repo root, or `docs/`; **first location found wins**; per-branch.
- **gitignore-style globs**, with three exceptions: `!` negation unsupported, `[ ]` character ranges unsupported, backslash-escaping a leading `#` unsupported. Case-sensitive.
- **The last matching pattern takes precedence** — one winner, not accumulation. Earlier matching lines are ignored entirely.
- Exclusion is expressed as a pattern with no owner:

```
/apps/        @octocat
/apps/github
```

- 3 MB limit; invalid lines are skipped and surfaced in the UI and REST API.

[GitLab's variant](https://docs.gitlab.com/user/project/codeowners/reference/) differs in ways that matter if you copy semantics. It has **sections**:

```
[Documentation] @docs-team
docs/
README.md
```

Section names are case-insensitive and duplicates are combined. `^[Section name]` marks a section optional. `[Section name][5]` requires five approvals; `[Section name][2] @group` combines a count with default owners. Matching is **last-match within each section, accumulating across sections** — which is a genuinely different and, for our purposes, better model than GitHub's single-winner rule. GitLab also supports `!` exclusions (scoped to a section) and treats non-`/`-prefixed paths as globstar-at-any-depth.

If you want one battle-tested mental model to reuse: **an ordered list of gitignore-style globs, last match wins, empty-owner line means "unset", sections accumulate.**

### 4.6 Other path→metadata conventions in real use

| Convention | Maps | Adoption | URL |
|---|---|---|---|
| `.gitattributes` | glob → attributes (`linguist-generated`, `merge=`, `diff=`) | Heavy | https://git-scm.com/docs/gitattributes |
| GH Actions `paths:` / `paths-ignore:` | globs → whether a workflow runs | Heavy | https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax |
| `actions/labeler` (`.github/labeler.yml`) | globs → PR labels | Wide | https://github.com/actions/labeler |
| Renovate `packageRules.matchFileNames` | globs → update policy | Wide | https://docs.renovatebot.com/configuration-options/ |
| Nx / Bazel project boundaries | directory → project identity + dependency rules | Heavy in monorepos | https://nx.dev, https://bazel.build |
| Danger path rules | changed paths → review assertions | Moderate | https://danger.systems |
| Claude Code `.claude/rules/*.md` `paths:` frontmatter | globs → doc loaded when a matching file is read | Vendor feature | https://code.claude.com/docs/en/memory |

`actions/labeler` is the nearest analogue to "glob → tag":

```yaml
label-name:
  - changed-files:
      - any-glob-to-any-file: ['list','of','globs']
```

(also `any-glob-to-all-files`, `all-globs-to-any-file`, `all-globs-to-all-files`, with `any:`/`all:` nesting.)

Note the structural split. CODEOWNERS and labeler map **path → metadata**, with the index living outside the documents. adrkit maps **document → paths**, with the index living in each document's frontmatter. Both shapes exist; only the first has a mature precedent, but only the second keeps the mapping next to the thing it describes (and is therefore regenerable, PEP-style).

### 4.7 Documentation maps

Google's answer, documented in [*Software Engineering at Google* ch. 10](https://abseil.io/resources/swe-book/html/ch10.html) and the [docguide](https://google.github.io/styleguide/docguide/best_practices.html), is `g3doc/` directories alongside the code they document, in Markdown, reviewed like code. That is **containment, not indexing**. A README.md per directory is a de facto norm, not a specification. No primary-source convention was found for a machine-readable file mapping directories to docs.

---

## 5. Agent and LLM context engineering

This is the one area where the industry has genuinely converged, and it converged on the shape we want.

### 5.1 Progressive disclosure

Anthropic's [Effective context engineering for AI agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) frames it as *just-in-time context*, explicitly against pre-loading:

> "agents built with the 'just-in-time' approach maintain lightweight identifiers (file paths, stored queries, web links, etc.) and use these references to dynamically load data into context at runtime using tools"

> "Context… must be treated as a finite resource with diminishing marginal returns… Every new token introduced depletes this budget by some amount"

It names progressive disclosure ("allows agents to incrementally discover relevant context through exploration"), compaction, and sub-agents returning "only a condensed, distilled summary". No numeric budget is given.

[Equipping agents for the real world with Agent Skills](https://www.anthropic.com/engineering/equipping-agents-for-the-real-world-with-agent-skills) states the three-level mechanism precisely:

1. "At startup, the agent pre-loads the `name` and `description` of every installed skill into its system prompt" — "the **first level** of progressive disclosure: it provides just enough information for Claude to know when each skill should be used without loading all of it into context."
2. "If Claude thinks the skill is relevant to the current task, it will load the skill by reading its full `SKILL.md` into context."
3. Bundled files referenced by name, read only as needed; scripts executed rather than loaded.

The claim: "the amount of context that can be bundled into a skill is effectively unbounded" because cost is paid only on read.

### 5.2 Skill frontmatter — the relevance-selection mechanism, with real numbers

The vendor-neutral [agentskills.io specification](https://agentskills.io/specification.md):

| Field | Required | Constraint |
|---|---|---|
| `name` | yes | 1–64 chars, lowercase alnum + hyphens, must match parent directory name |
| `description` | yes | 1–**1024** chars, "what it does and when to use it" |
| `license` | no | — |
| `compatibility` | no | max 500 chars |
| `metadata` | no | string→string map |
| `allowed-tools` | no | space-separated, experimental |

Stated budgets: metadata **~100 tokens** loaded at startup for all skills; instructions **"< 5000 tokens recommended"**, loaded on activation; resources loaded only when required. "Keep your main SKILL.md under 500 lines." "Keep file references one level deep from SKILL.md. Avoid deeply nested reference chains."

[Anthropic's authoring guidance](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices) adds directly applicable rules:

- Descriptions must be **third person** (they are injected into the system prompt); "Claude uses it to choose the right Skill from potentially 100+ available Skills."
- Its "domain-organized index" pattern is literally index-then-zoom: SKILL.md is a link table to `reference/finance.md`, `reference/sales.md`, with suggested `grep -i "revenue" reference/finance.md`.
- **"For reference files longer than 100 lines, include a table of contents at the top"** — because Claude may preview with `head -100` rather than read the whole file.
- "No context penalty for large files… until actually read."

One caveat that matters for our sizing: [Claude Code's large-codebases guide](https://code.claude.com/docs/en/large-codebases) notes **"descriptions are shortened when there are many"** — the 1024-char budget is not guaranteed to survive at scale. "Keep descriptions short and lead with words a request would contain." That is a direct argument for keyword-dense, front-loaded index lines.

### 5.3 AGENTS.md and CLAUDE.md

[AGENTS.md](https://agents.md/) is a deliberately thin convention: a file "at the root of the repository"; "just standard Markdown. Use any headings you like; the agent simply parses the text you provide." **No required fields, no frontmatter, no schema.** For monorepos: "Place another AGENTS.md inside each package. Agents automatically read the nearest file in the directory tree, so the closest one takes precedence" — stated as a claim about agent behavior, not an enforceable spec.

[CLAUDE.md](https://code.claude.com/docs/en/memory) is one vendor's product behavior but is far more specified, and two of its mechanisms are directly relevant:

- Discovery walks **up** the tree from cwd, concatenating. Files in **subdirectories are not loaded at launch** — "they are included when Claude reads files in those subdirectories." That is itself a lazy path-triggered loading mechanism.
- Imports use `@path/to/import`, max **four hops**. Crucially, **imports do not save context** — "imported files still load and enter the context window at launch."
- Size guidance: **"target under 200 lines per CLAUDE.md file. Longer files consume more context and reduce adherence."**
- **`.claude/rules/*.md` with `paths:` glob frontmatter** — conditional loading triggered when Claude reads a matching file. This is the closest thing in the agent world to a path→document mapping, and it is a shipped vendor feature rather than a convention.
- **Auto memory** is the closest native "index file": `~/.claude/projects/<project>/memory/MEMORY.md` is described as "an index of the memory directory", with a hard enforced load limit of **the first 200 lines or 25KB, whichever comes first**; topic files beside it are read on demand. Over-limit writes error and instruct a rewrite. This is a working, enforced instance of exactly the pattern we are designing.

### 5.4 Retrieval vs curated index — honestly contested

The strongest claims on both sides come from vendors evaluating their own products. There is no neutral head-to-head.

**For agentic search / grep:** Anthropic's context-engineering post argues for just-in-time identifiers over pre-processing, citing staleness and irrelevant retrieval as the failure modes of pre-indexing. Claude Code's large-codebase guide never recommends building an embedding index; its recommended escalation from grep is a **language server**, and it mentions RAG only as "if your organization already runs a code search or RAG index over the repository, expose it as an MCP tool."

**For semantic search:** Cursor's [Improving agent with semantic search](https://cursor.com/blog/semsearch) reports measured, self-evaluated results — **+12.5% average accuracy** (6.5%–23.5% by model) vs grep/CLI tools alone; 2.2% more dissatisfaction-requiring follow-ups without it; code retention +0.3% overall and **+2.6% on codebases with 1,000+ files**. Their own conclusion is *not* "embeddings beat grep": "the agent makes heavy use of grep as well as semantic search, and the combination of these two leads to the best outcomes," with semantic search mattering more as the codebase grows. Cursor also publishes the opposite-direction argument that the win is a faster exact-match index ([Fast regex search](https://cursor.com/blog/fast-regex-search)).

**A third option — a cheap deterministic structural index:** [Aider's repo map](https://aider.chat/docs/repomap.html) parses each file with tree-sitter to find "where functions, classes, variables, types and other definitions occur", then ranks with "a graph ranking algorithm, computed on a graph where each source file is a node and edges connect files which have dependencies," keeping "only the most important identifiers, the ones which are most often referenced by other portions of the code." **Token budget: `--map-tokens` defaults to 1k tokens**, expanding "significantly at times, especially when no files have been added to the chat." (Caveat: the docs say "graph ranking algorithm"; PageRank is what the implementation uses, but do not cite the docs for that word.)

Also flag as **unverified**: the widely-circulated "Claude Code removed vector search" narrative and the Boris Cherny "outperformed everything, by a lot" quote could not be traced to an Anthropic primary source. Do not cite them.

### 5.5 llms.txt — the curated flat link index

[llmstxt.org](https://llmstxt.org/) (Jeremy Howard / Answer.AI; widely adopted, not a standards-body spec). Its rationale is our exact problem: "Large language models increasingly rely on website information, but face a critical limitation: context windows are too small to handle most websites in their entirety."

Structure at `/llms.txt`, in order:

1. An **H1** with the project name — "This is the only required section."
2. A **blockquote** with a short summary containing key information.
3. Zero or more markdown sections of any type except headings.
4. Zero or more **H2 sections containing file lists** — bullet items of "a required markdown hyperlink `[name](url)`, then optionally a `:` and notes about the file."

The `## Optional` H2 is special: its URLs "can be skipped if a shorter context is needed. Use it for secondary information which can often be skipped." That is an explicit priority tier for a context-constrained reader — a mechanism worth copying directly.

Note: `llms-ctx.txt` / `llms-ctx-full.txt` are the site's own tool-generated expansions; **`llms-full.txt` is a community convention, not something the format section defines.**

This pattern is deployed in production: Anthropic, MCP, and agentskills.io docs each prepend a blockquote to fetched pages saying "Fetch the complete documentation index at: … Use this file to discover all available pages before exploring further."

### 5.6 MCP resources

[MCP resources](https://modelcontextprotocol.io/docs/concepts/resources) are two-phase by construction: `resources/list` returns lightweight descriptors (`uri`, `name`, optional `title`, `description`, `mimeType`, `size`, `icons`), and only `resources/read` returns contents.

Two details worth stealing:

- **`annotations.priority`** — a float 0.0–1.0, "1 means most important (effectively required), 0 means least important (entirely optional)"; the spec says clients can use it to "prioritize which resources to include in context." A numeric version of llms.txt's `## Optional`.
- **Resource templates** (RFC 6570 URI templates) let a server advertise a parameterized address space without enumerating it — relevant if an index would otherwise be combinatorially large.

Caveat the spec states itself: resources are "application-driven"; the protocol does not mandate that the *model* picks.

---

## 6. The main question: is there a convention for mapping a decision to an area of the codebase?

**No. There is no established convention. Anything built here is designing a convention, not adopting one.**

That answer holds across every body of practice surveyed:

- **Nygard** — no. Sequential numbers and a status word.
- **MADR** — no. Prose instruction to "reference architectural components" in the Context section. No field. No tags either.
- **adr-tools / adr-log / adr-viewer / adr-manager** — no. Every generated index is `link + title`.
- **arc42** — no. Section 5's only traceability field points at *requirements*. The official answer to scale is a tagged blog.
- **C4** — no. Silent on decisions entirely.
- **IETF / Rust / PEP / Oxide** — not applicable; these govern specs, not codebases. None maps a document to source paths.
- **Google** — containment (`g3doc/` beside the code), not indexing.

Here is the full ranked list of what does exist, most to least mature:

| Analogue | What it maps | Maturity | Gap for our purpose |
|---|---|---|---|
| CODEOWNERS (GitHub/GitLab) | path glob → owners | Universal, battle-tested semantics | Wrong payload — owners, not decisions |
| `actions/labeler`, Actions `paths:`, `.gitattributes`, Renovate | path glob → label / behavior / attribute | Wide | Wrong payload |
| Claude Code `.claude/rules/*.md` `paths:` | path glob → document loaded on read | Shipped vendor feature | Single-vendor; loads prose, not a pointer index |
| Structurizr `!adrs` | ADR *folder* → workspace / system / container | Mature tool, real usage | Coarse: folder-level, model elements not file paths, no component scope |
| log4brains `packages` | package directory → ADR folder | One mainstream tool | Containment scoping only; no per-decision mapping |
| MADR category subfolders | subfolder → loose category | Explicitly non-normative suggestion | Breaks global ID uniqueness; not paths |
| e-ADR `@ADR(n)` | Java class → ADR number | Published, negligible adoption | Code→decision, one class at a time; no reverse index |
| adrkit `affects:` | decision → path globs + package constraints | v0.3.0, ~5 stars, months old | Exactly the idea; no adoption, no stability |

Note the two structural shapes, because we must pick one:

- **path → decision** (CODEOWNERS shape): the index is a separate ordered file of globs. Proven semantics, single lookup, but the mapping lives away from the record and can drift.
- **decision → paths** (adrkit shape): each record declares an `affects` list in frontmatter. The mapping lives next to what it describes, and the index becomes a **derived artifact** — the PEP property that makes drift structurally impossible.

The evidence favors the second shape with the first shape's matching semantics: declare scope on the record, generate the path-ordered index, and use gitignore-style globs with accumulating (GitLab-style) matching rather than GitHub's last-match-wins, since multiple decisions legitimately govern one file.

Two further things the industry does have, that our design should not reinvent:

1. **A bidirectional supersession pair with a separate status field** (IETF's Obsoletes/Obsoleted-by and Updates/Updated-by; PEP's Replaces/Superseded-By plus a `Superseded` status). ADR practice collapses replacement and amendment into one word and pays for it — no ADR index anywhere can filter superseded records out.
2. **A validity state distinct from an agreement state** — Oxide's `committed` ("this is how the system actually works now") versus `published` ("we agreed to this"). This is the only primary-source mechanism found anywhere that answers "is this decision still true?"

And one budget anchor, since the consumer is an agent: the two empirically grounded numbers in the whole survey are **200 lines / 25KB** (Claude Code's enforced MEMORY.md index limit) and **~1k tokens** (Aider's default repo map). The convergent shape across five independent sources — Agent Skills, llms.txt, MCP resources, MEMORY.md, Aider — is a small always-loaded index of one-line, keyword-dense, third-person descriptions, each paired with a stable address, with an explicit optional/priority tier, bodies exactly one hop away, and no nested reference chains.

---

## Sources

**ADRs:** [Nygard 2011](https://www.cognitect.com/blog/2011/11/15/documenting-architecture-decisions) · [Fowler bliki](https://martinfowler.com/bliki/ArchitectureDecisionRecord.html) · [MADR](https://adr.github.io/madr/) · [madr repo](https://github.com/adr/madr) · [adr-tools](https://github.com/npryce/adr-tools) · [adr-log](https://github.com/adr/adr-log) · [log4brains](https://github.com/thomvaill/log4brains) · [adr-viewer](https://github.com/mrwilson/adr-viewer) · [adr-manager](https://github.com/adr/adr-manager) · [adr.github.io](https://adr.github.io/) · [adr tooling list](https://adr.github.io/adr-tooling/) · [ad-practices](https://adr.github.io/ad-practices/) · [e-ADR](http://adr.github.io/e-adr/) · [adrkit](https://github.com/mbeacom/adrkit) · [ThoughtWorks Radar](https://www.thoughtworks.com/radar/techniques/lightweight-architecture-decision-records) · [Azure WAF](https://learn.microsoft.com/en-us/azure/well-architected/architect-role/architecture-decision-record) · [AWS Prescriptive Guidance](https://docs.aws.amazon.com/prescriptive-guidance/latest/architectural-decision-records/best-practices.html) · [Zimmermann, creating ADRs](https://ozimmer.ch/practices/2023/04/03/ADRCreation.html) · [Zimmermann, reviewing ADRs](https://ozimmer.ch/practices/2023/04/05/ADRReview.html)

**arc42 / C4:** [arc42 §9](https://docs.arc42.org/section-9/) · [arc42 §5](https://docs.arc42.org/section-5/) · [arc42 tip 9-5](https://docs.arc42.org/tips/9-5/) · [arc42 FAQ category C](https://faq.arc42.org/category_c/) · [FAQ C-9-4](https://faq.arc42.org/questions/C-9-4/) · [c4model.com](https://c4model.com/) · [C4 FAQ](https://c4model.com/faq) · [Structurizr DSL](https://docs.structurizr.com/dsl/language) · [Structurizr ADRs](https://docs.structurizr.com/dsl/adrs) · [Structurizr decisions UI](https://docs.structurizr.com/ui/decisions/)

**RFC processes:** [RFC index XML](https://www.rfc-editor.org/rfc-index.xml) · [RFC 2616 info page](https://www.rfc-editor.org/info/rfc2616/) · [RFC 9110](https://www.rfc-editor.org/rfc/rfc9110.txt) · [rust-lang/rfcs README](https://raw.githubusercontent.com/rust-lang/rfcs/master/README.md) · [Rust RFC Book](https://rust-lang.github.io/rfcs/) · [PEP 1](https://peps.python.org/pep-0001/) · [PEP 0](https://peps.python.org/pep-0000/) · [PEP rendering system](https://peps.python.org/docs/rendering_system/) · [Oxide RFD 1](https://rfd.shared.oxide.computer/rfd/0001) · [Oxide blog](https://oxide.computer/blog/rfd-1-requests-for-discussion) · [Squarespace](https://engineering.squarespace.com/blog/2019/the-power-of-yes-if)

**Docs-as-code:** [Living Documentation workshop](https://github.com/cyriux/livingdocumentation-workshop) · [InfoQ Q&A](https://www.infoq.com/articles/book-review-living-documentation/) · [GitHub CODEOWNERS](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-code-owners) · [GitLab CODEOWNERS](https://docs.gitlab.com/user/project/codeowners/reference/) · [actions/labeler](https://github.com/actions/labeler) · [gitattributes](https://git-scm.com/docs/gitattributes) · [SWE at Google ch.10](https://abseil.io/resources/swe-book/html/ch10.html) · [Google docguide](https://google.github.io/styleguide/docguide/best_practices.html)

**Agent context:** [Effective context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) · [Agent Skills](https://www.anthropic.com/engineering/equipping-agents-for-the-real-world-with-agent-skills) · [agentskills.io spec](https://agentskills.io/specification.md) · [Skills best practices](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices) · [Claude Code skills](https://code.claude.com/docs/en/skills) · [Large codebases](https://code.claude.com/docs/en/large-codebases) · [CLAUDE.md memory](https://code.claude.com/docs/en/memory) · [AGENTS.md](https://agents.md/) · [llms.txt](https://llmstxt.org/) · [MCP resources](https://modelcontextprotocol.io/docs/concepts/resources) · [Aider repo map](https://aider.chat/docs/repomap.html) · [Cursor semantic search](https://cursor.com/blog/semsearch) · [Cursor regex search](https://cursor.com/blog/fast-regex-search)
