"""Configuration loading for personal_mem.

Priority: env vars > config.toml > defaults.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


_DEFAULT_VAULT = Path.home() / "vault"


@dataclass
class Config:
    vault_root: Path = field(default_factory=lambda: _DEFAULT_VAULT)
    default_project: str = ""
    embedding_model: str = "text-embedding-3-small"
    embedding_api_key_env: str = "OPENAI_API_KEY"  # env var name holding the key
    embedding_api_url: str = "https://api.openai.com/v1/embeddings"

    # Edge generation thresholds
    concept_edge_threshold: int = 1  # min shared concepts for relates_to edge
    concept_edge_max_freq_pct: float = 0.05  # skip concepts in >5% of notes
    tag_edge_threshold: int = 2  # min shared tags for relates_to edge
    tag_edge_max_freq_pct: float = 0.10  # skip tags in >10% of notes
    tag_edge_exclude: tuple[str, ...] = ("todo", "probe", "parked", "til")

    @property
    def mem_dir(self) -> Path:
        return self.vault_root / ".mem"

    @property
    def index_db(self) -> Path:
        return self.mem_dir / "index.db"

    @property
    def embeddings_db(self) -> Path:
        return self.mem_dir / "embeddings.db"

    @property
    def config_path(self) -> Path:
        return self.mem_dir / "config.toml"

    @property
    def templates_dir(self) -> Path:
        return self.vault_root / "templates"


def load_config() -> Config:
    """Load config from env vars, then config.toml, then defaults."""
    cfg = Config()

    # Env vars take highest priority
    vault_env = os.environ.get("PERSONAL_MEM_VAULT")
    if vault_env:
        cfg.vault_root = Path(vault_env)

    # Try to read config.toml
    toml_path = cfg.config_path
    if toml_path.exists():
        with open(toml_path, "rb") as f:
            data = tomllib.load(f)
        if "vault_root" in data and not vault_env:
            cfg.vault_root = Path(data["vault_root"])
        if "default_project" in data:
            cfg.default_project = data["default_project"]
        embed = data.get("embeddings", {})
        if "model" in embed:
            cfg.embedding_model = embed["model"]
        if "api_key_env" in embed:
            cfg.embedding_api_key_env = embed["api_key_env"]
        if "api_url" in embed:
            cfg.embedding_api_url = embed["api_url"]

        # Edge generation config
        edges = data.get("edges", {})
        if "concept_threshold" in edges:
            cfg.concept_edge_threshold = int(edges["concept_threshold"])
        if "concept_max_freq_pct" in edges:
            cfg.concept_edge_max_freq_pct = float(edges["concept_max_freq_pct"])
        if "tag_threshold" in edges:
            cfg.tag_edge_threshold = int(edges["tag_threshold"])
        if "tag_max_freq_pct" in edges:
            cfg.tag_edge_max_freq_pct = float(edges["tag_max_freq_pct"])
        if "tag_exclude" in edges:
            cfg.tag_edge_exclude = tuple(edges["tag_exclude"])

    # Per-field env overrides
    if os.environ.get("PERSONAL_MEM_PROJECT"):
        cfg.default_project = os.environ["PERSONAL_MEM_PROJECT"]
    if os.environ.get("PERSONAL_MEM_DB"):
        # Override index db path directly
        cfg._index_db_override = Path(os.environ["PERSONAL_MEM_DB"])

    return cfg
