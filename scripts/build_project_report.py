#!/usr/bin/env python3
"""Regenerate the self-contained LangGraph Customer Support Agent project report."""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORT_SKILL = Path("/Users/qifanwen/.claude/skills/html-report/scripts")
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(REPORT_SKILL))

import report as R  # noqa: E402

from support_copilot.graph import GraphDependencies, build_graph  # noqa: E402


class FakeModel:
    """Shape-only dependency: graph rendering never calls model methods."""

    async def structured(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("Rendering must not invoke the model")

    async def generate(self, *args: object, **kwargs: object) -> str:
        raise AssertionError("Rendering must not invoke the model")

    async def embed(self, *args: object, **kwargs: object) -> list[list[float]]:
        raise AssertionError("Rendering must not invoke the model")


class FakeRepository:
    pass


class FakeCache:
    pass


def render_mermaid(source: str, name: str, scale: int = 1) -> bytes:
    """Render Mermaid source locally, leaving no figure sidecar behind."""
    with tempfile.TemporaryDirectory() as directory:
        work = Path(directory)
        source_path = work / f"{name}.mmd"
        output_path = work / f"{name}.png"
        source_path.write_text(source, encoding="utf-8")
        subprocess.run(
            [
                "mmdc",
                "-i",
                str(source_path),
                "-o",
                str(output_path),
                "-b",
                "transparent",
                "-w",
                "1500",
                "-s",
                str(scale),
            ],
            check=True,
            cwd=ROOT,
        )
        return output_path.read_bytes()


def graph_image() -> bytes:
    graph = build_graph(GraphDependencies(FakeModel(), FakeRepository(), FakeCache()))
    # draw_mermaid() is LangGraph's own rendering representation of the compiled graph.
    mermaid = graph.get_graph().draw_mermaid().replace("graph TD;", "graph LR;")
    return render_mermaid(mermaid, "langgraph")


def schema_columns(schema: str, table: str) -> list[tuple[str, str, str]]:
    """Extract displayed columns and key annotations from the real SQL schema."""
    match = re.search(rf"CREATE TABLE IF NOT EXISTS {table} \((.*?)\n\);", schema, flags=re.DOTALL)
    if match is None:
        raise ValueError(f"Missing {table} in schema.sql")
    body = match.group(1)
    composite_unique = {
        column.strip()
        for group in re.findall(r"UNIQUE \(([^)]+)\)", body)
        for column in group.split(",")
    }
    columns = []
    for raw_line in body.splitlines():
        line = raw_line.strip().rstrip(",")
        if not line or line.startswith(("UNIQUE", "CONSTRAINT", "CHECK")):
            continue
        match = re.match(r"(\w+)\s+([A-Z]+|vector)(?:\([^)]*\))?\s+(.+)", line)
        if match is None:
            continue
        name, data_type, rest = match.groups()
        keys = []
        if "PRIMARY KEY" in rest:
            keys.append("PK")
        if "REFERENCES" in rest:
            keys.append("FK")
        if "UNIQUE" in rest or name in composite_unique:
            keys.append("UK")
        columns.append((data_type, name, ", ".join(keys)))
    return columns


def er_source() -> str:
    schema = (ROOT / "src/support_copilot/schema.sql").read_text(encoding="utf-8")
    tables = [
        "customers",
        "products",
        "orders",
        "runtime_settings",
        "thread_owners",
        "help_document_embeddings",
    ]
    blocks = []
    for table in tables:
        fields = "\n".join(
            f"        {data_type} {name} {keys}".rstrip()
            for data_type, name, keys in schema_columns(schema, table)
        )
        blocks.append(f"    {table.upper()} {{\n{fields}\n    }}")
    blocks.append(
        '    LANGGRAPH_CHECKPOINTS {\n        TEXT state "Library-owned checkpoint tables"\n    }'
    )
    return "\n".join(
        [
            "erDiagram",
            *blocks,
            "    CUSTOMERS ||--o{ ORDERS : places",
            "    PRODUCTS ||--o{ ORDERS : contains",
        ]
    )


def tags(items: list[str]) -> str:
    """Render the report's compact project-tag row."""
    return (
        '<div class="tags" aria-label="Project tags">'
        + "".join(f'<span class="tag-chip">{item}</span>' for item in items)
        + "</div>"
    )


def bullet_list(items: list[str], class_name: str | None = None) -> str:
    """Render a plain unordered list with an optional report style class."""
    class_attr = f" class='{class_name}'" if class_name else ""
    return f"<ul{class_attr}>" + "".join(f"<li>{item}</li>" for item in items) + "</ul>"


def numbered_section(number: int, title: str) -> str:
    """Keep section numbers in both headings and contents-rail links."""
    return R.section(f"{number} {title}")


def main() -> None:
    langgraph_png = graph_image()
    er_png = render_mermaid(er_source(), "database-er")

    R.glossary(
        {
            "LangGraph": {
                "category": "workflow library",
                "expansion": "LangGraph",
                "meaning": {
                    "role": "A graph runtime that persists state and can interrupt then resume a workflow."
                },
                "example": "The compiled graph has 6 named nodes: extract, route, rag, sql, refund, and respond.",
            },
            "RAG": {
                "category": "retrieval pattern",
                "expansion": "Retrieval-augmented generation",
                "meaning": {
                    "role": "Retrieves relevant source text before a model drafts an answer from that evidence."
                },
                "example": "One of 3 route handlers retrieves chunks from the 10 store help documents.",
            },
            "Postgres": {
                "category": "database",
                "expansion": "PostgreSQL relational database",
                "meaning": {
                    "role": "Stores relational records, vectors, and durable workflow checkpoints in this application."
                },
                "example": "One Postgres instance holds application data, help embeddings, and checkpoint state.",
            },
            "pgvector": {
                "category": "vector extension",
                "expansion": "pgvector",
                "meaning": {
                    "role": "A PostgreSQL extension that stores embedding vectors and supports similarity search."
                },
                "example": "The schema declares one embedding vector column per help-document chunk.",
            },
            "Redis": {
                "category": "in-memory data store",
                "expansion": "Redis",
                "meaning": {
                    "role": "Carries queued jobs, run-status records, locks, and short-lived SQL results."
                },
                "example": "Generated SQL results are cached for 300 seconds before they are queried again.",
            },
            "arq": {
                "category": "job queue",
                "expansion": "arq",
                "meaning": {"role": "A Python asynchronous job queue backed by Redis."},
                "example": "A submitted run and its later refund decision are separate queued jobs.",
            },
            "Pydantic": {
                "category": "data validation",
                "expansion": "Pydantic",
                "meaning": {
                    "role": "Python models that validate API payloads and define structured model outputs."
                },
                "example": "The run status contract has 5 allowed states: queued, running, awaiting_approval, completed, and failed.",
            },
        }
    )

    R.CSS += (
        ".tags{display:flex;flex-wrap:wrap;gap:.35rem;margin:.9rem 0 0}"
        '.tag-chip{font-family:"SFMono-Regular",Consolas,"Liberation Mono",Menlo,monospace;'
        "font-size:.62rem;line-height:1.5;padding:.1rem .45rem;border-radius:999px;"
        "color:#7c3043;background:#f5eaed;border:1px solid #dcc2c9}"
        ".summary-bullets{margin:1rem 0 0}"
    )

    parts = [
        tags(
            [
                "LangGraph",
                "RAG",
                "FastAPI",
                "PostgreSQL",
                "AWS EC2",
                "Human-in-the-loop",
                "CI/CD",
                "Docker",
            ]
        ),
        bullet_list(
            [
                "Built a customer-support agent for a mock online movie-media store. A LangGraph graph extracts structured fields from each message using OpenRouter structured output against Pydantic schemas, routes it to one of three handlers, answers policy questions with RAG over pgvector and data questions with model-written read-only SQL against Postgres, and pauses refund requests for human approval with interrupt() and a Postgres checkpointer.",
                "Kept the graph out of the web request: FastAPI returns a run id immediately, an arq worker runs the graph off a Redis queue, and clients poll run status. The HTTP client, Redis connection, and Postgres pool are created once at startup and reused across requests.",
                "Built a React and TypeScript console with a customer portal (identify by name and email, pick an order, track refund progress) and an employee console (monitor runs, approve or reject refunds, edit the business tables).",
                "Exported the backend Pydantic schemas as a JSON contract that generates the frontend TypeScript types, with a test that fails when the two sides drift.",
                "Deployed the full stack with Docker Compose on an AWS EC2 instance.",
                "Wrote unit tests with the model faked, an end-to-end test of the submit, pause, and approve flow, and a 20-example labeled set scoring extraction and routing accuracy. CI runs lint, tests, and the Docker build on every push.",
            ],
            "summary-bullets",
        ),
        R.note(
            "Purpose: show how an asynchronous support workflow keeps evidence, routing, and a refund decision inspectable."
        ),
        numbered_section(1, "Motivation"),
        R.para(
            "A physical-media store receives policy questions, order questions, sales questions, and refund requests in the same inbox. "
            "The product separates those paths while retaining the evidence and the decision boundary needed for a support agent to review the work."
        ),
        numbered_section(2, "One message path"),
        R.para(
            "A customer message moves through five stages: <b>1. extract</b> structured fields such as issue type and order number; "
            "<b>2. route</b> to a lane and handler; <b>3. gather evidence</b> with RAG, a read-only SQL query, or a refund proposal; "
            "<b>4. pause</b> only when a refund needs approval; and <b>5. respond</b> from the selected evidence and recent thread context."
        ),
        R.figure(langgraph_png, "Figure 1. Compiled LangGraph message workflow."),
        numbered_section(3, "Architecture"),
        R.para(
            "The React console submits a run to FastAPI, which records a queued state and returns HTTP 202. Redis and arq carry the job to a separate worker, where the LangGraph workflow runs. "
            f"Keeping graph execution off the web request makes the API responsive and allows the worker to own retries, thread locking, and resume work {R.cite(1)}."
        ),
        R.para(
            "A refund proposal calls the graph interrupt rather than issuing a payment. The worker writes <code>awaiting_approval</code>, and a later approve or reject request enqueues a second job that resumes the same thread checkpoint. "
            "This creates a durable human decision point instead of treating a refund as an ordinary generated answer."
        ),
        numbered_section(4, "Data layer"),
        R.para(
            "One Postgres instance holds application tables for business records, the employee-controlled daily run limit, and thread owners; searchable help-document embeddings through pgvector; and LangGraph checkpoint tables. "
            f"The application stores one owner for each support thread, while LangGraph creates and owns its checkpoint tables {R.cite(2)}."
        ),
        R.figure(er_png, "Figure 2. Postgres entity relationship diagram."),
        numbered_section(5, "Shared API contract"),
        R.para(
            f"The backend Pydantic response models and the React console meet at <code>web/api-contract.json</code>. A generator emits TypeScript types from that inventory, while a backend test reconstructs the same inventory from the Pydantic models and fails when it drifts {R.cite(3)}."
        ),
        R.table(
            ["Endpoint", "Boundary"],
            [
                [
                    "<code>POST /runs</code>",
                    "Accept a message and return a run and thread identifier with 202.",
                ],
                [
                    "<code>GET /runs/{id}</code>",
                    "Poll the typed run state, extraction, route, answer, or refund proposal.",
                ],
                ["<code>GET /runs?status=awaiting_approval</code>", "List the approval inbox."],
                ["<code>POST /runs/{id}/decision</code>", "Queue an approve or reject resume job."],
            ],
            caption="Table 1. Run API contract.",
            align="ll",
        ),
        numbered_section(6, "Evaluation"),
        R.para(
            "The labeled evaluation set contains 20 support messages. It measures exact issue-type extraction and joint route accuracy, where both the support lane and handler must match the label. "
            "The automated acceptance floor is 75% for each score; the model evaluation runs only when an API key is supplied."
        ),
        R.table(
            ["Check", "What it measures"],
            [
                ["Extraction", "Exact match for the labeled issue type."],
                ["Routing", "Exact match for the labeled lane and handler together."],
                [
                    "Coverage",
                    "Policy, order, refund, and aggregate sales requests across 20 messages.",
                ],
            ],
            caption="Table 2. Labeled evaluation coverage.",
            align="ll",
        ),
        numbered_section(7, "Limitations"),
        R.olist(
            [
                "Refunds are simulated against fake business data and do not integrate with a payment provider.",
                "The demo has no authentication, authorization, or tenant isolation. Its global UTC-day run cap is the deliberate exception while per-user tracking, API-key authentication, token accounting, and general rate limiting remain future work.",
                "The console polls run status rather than streaming progress to the browser.",
                "Thread context is intentionally minimal: the response node reads only the six most recent stored messages.",
            ]
        ),
        numbered_section(8, "Conclusion"),
        R.para(
            "Portfolio Support Copilot is a compact demonstration of an agent workflow with typed inputs, explicit routing, evidence-specific handlers, and a durable approval gate. "
            "Its boundaries are visible in the API, graph, database, and evaluation rather than hidden behind a single chat response. "
            f"The source code is public {R.cite(4)}."
        ),
        numbered_section(9, "References"),
        R.references(
            [
                "LangChain, <i>LangGraph Documentation</i>, 2026.",
                "pgvector contributors, <i>pgvector Documentation</i>, 2026.",
                "Pydantic, <i>Pydantic Documentation</i>, 2025.",
                'Q. Wen, <i>portfolio-support-copilot</i>, GitHub repository, 2026. <a class="cite" href="https://github.com/Albatross679/portfolio-support-copilot" target="_blank" rel="noopener noreferrer">github.com/Albatross679/portfolio-support-copilot</a>',
            ]
        ),
    ]
    R.build(
        "LangGraph Customer Support Agent",
        "Qifan Wen · qifanwen679@gmail.com · August 2026",
        parts,
        ROOT / "reports/project-report.html",
    )


if __name__ == "__main__":
    main()
