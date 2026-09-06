"""Extract quoted project observations; neither execute code nor infer accomplishments."""

import ast
import re
from pathlib import PurePosixPath
from typing import Any

LANGUAGES = {
    ".py": "Python",
    ".cpp": "C++",
    ".cc": "C++",
    ".js": "JavaScript",
    ".ts": "TypeScript",
    ".rs": "Rust",
    ".java": "Java",
}
LIBRARIES = {
    "torch": "PyTorch",
    "tensorflow": "TensorFlow",
    "numpy": "NumPy",
    "pandas": "pandas",
    "sklearn": "scikit-learn",
    "cv2": "OpenCV",
    "rclpy": "ROS 2",
    "fastapi": "FastAPI",
    "sqlalchemy": "SQLAlchemy",
}


def selected(path: str, mode: str, size: int) -> bool:
    parsed = PurePosixPath(path)
    if parsed.is_absolute() or any(
        p in {"..", "node_modules", "vendor", "dist"} or p.startswith(".") for p in parsed.parts
    ):
        return False
    return (
        mode in {"100644", "100755"}
        and 0 < size <= 200_000
        and (
            parsed.suffix.lower() in {*LANGUAGES, ".md", ".toml", ".json", ".yaml", ".yml"}
            or parsed.name.lower() in {"requirements.txt", "cmakelists.txt", "package.xml"}
        )
    )


def observations(path: str, text: str) -> list[dict[str, Any]]:
    """Return exact quotations. Numerical README claims remain unverified quotations."""
    result: list[dict[str, Any]] = []
    lines = text.splitlines()
    name = PurePosixPath(path).name.lower()

    def add(number: int, statement: str, skill: str | None = None) -> None:
        line = lines[number - 1]
        if line.strip() and len(line) <= 2000:
            result.append({"line": number, "quote": line, "statement": statement, "skill": skill})

    if name.startswith("readme"):
        for number, line in enumerate(lines, 1):
            if line.strip() and not line.lstrip().startswith(("#", "```", "![", "<")):
                add(number, f"README states: {line}")
                if len(result) >= 20:
                    break
    language = LANGUAGES.get(PurePosixPath(path).suffix)
    if language:
        first_line = next((n for n, line in enumerate(lines, 1) if line.strip()), None)
        if first_line:
            add(first_line, f"Repository contains a {language} source file: {path}.", language)
    if path.endswith(".py"):
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError):
            return result
        found: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            modules = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
            for module in modules:
                skill = LIBRARIES.get(module.split(".")[0])
                if skill and skill not in found:
                    found.add(skill)
                    add(
                        node.lineno,
                        f"Source imports {skill}; candidate proficiency is not established.",
                        skill,
                    )
    elif name in {"requirements.txt", "pyproject.toml", "package.json", "package.xml"}:
        for number, line in enumerate(lines, 1):
            for package, skill in LIBRARIES.items():
                if re.search(rf"(?<![\w-]){re.escape(package)}(?![\w-])", line):
                    add(
                        number,
                        f"Dependency manifest mentions {skill}; runtime use is not established.",
                        skill,
                    )
    return result[:40]
