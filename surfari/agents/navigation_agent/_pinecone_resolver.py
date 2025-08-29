from dataclasses import dataclass
from typing import Any, Optional
import os
from dotenv import load_dotenv
from pinecone import Pinecone
from surfari.agents.navigation_agent._typing import ResolveInput, ResolveOutput
import surfari.util.config as config
import surfari.util.surfari_logger as surfari_logger

logger = surfari_logger.getLogger(__name__)

# Load env
env_path = os.path.join(config.PROJECT_ROOT, "security", ".env_dev")
if not os.path.exists(env_path):
    env_path = os.path.join(config.PROJECT_ROOT, "security", ".env")
    logger.debug(f"PineconeResolver:Loading environment variables from {env_path}")
load_dotenv(dotenv_path=env_path)

@dataclass
class PineconeManagedEmbedResolver:
    """
    Pinecone resolver using Pinecone-managed embeddings.
    On upsert, include metadata["chunk_text"] with your raw text.
    On query, we ask Pinecone to embed the query text server-side, then search.
    """
    index: str
    embed_model: str = "llama-text-embed-v2"  # must match index managed model
    namespace: Optional[str] = None
    score_threshold: Optional[float] = None
    top_k: int = 3

    _pc: Any = None
    _pc_index: Any = None

    def __post_init__(self) -> None:
        api_key = os.getenv("PINECONE_API_KEY")
        if not api_key:
            raise RuntimeError("Missing PINECONE_API_KEY in your environment.")
        self._pc = Pinecone(api_key=api_key)
        self._pc_index = self._pc.Index(self.index)
        logger.info("Initialized Pinecone index=%r (managed model=%r)", self.index, self.embed_model)


    def resolve(self, inp: ResolveInput) -> ResolveOutput:
        """
        Use Pinecone-managed embeddings via Index.search().
        Expects search() result like:
        {"result": {"hits": [{"_id": "...", "_score": 7.8, "fields": {...}}, ...]}, "usage": {...}}
        """
        query_text = (inp.text or "").strip()
        if not query_text:
            return ResolveOutput(value=None)

        ns = self.namespace or ""

        # Ask Pinecone to embed + search server-side
        results = self._pc_index.search(
            namespace=ns,
            query={
                "inputs": {"text": query_text},
                "top_k": self.top_k,
            },
            # request back fields we stored during upsert
            fields=["chunk_text", "value", "label"],
        )
        # logger.debug("Pinecone search results: %r", results)
        # ---- normalize hits from the response ----
        # handle both dict and possible SDK object shapes
        if isinstance(results, dict):
            result_block = results.get("result") or {}
            hits = result_block.get("hits") or []
        else:
            # SDK object fallback
            try:
                result_block = getattr(results, "result", None) or {}
                hits = getattr(result_block, "hits", None) or []
            except Exception:
                hits = []

        if not hits:
            return ResolveOutput(value=None)

        # score helper (dict with "_score")
        def _score(hit: Any) -> float:
            if isinstance(hit, dict):
                return float(hit.get("_score", 0.0))
            # fallback if SDK object
            return float(getattr(hit, "_score", 0.0))

        best = max(hits, key=_score)
        best_score = _score(best)

        if self.score_threshold is not None and best_score < self.score_threshold:
            logger.debug("Best score %.4f below threshold %.4f", best_score, self.score_threshold)
            return ResolveOutput(value=None)

        # fields come back under "fields"
        fields = best.get("fields", {}) if isinstance(best, dict) else getattr(best, "fields", {}) or {}
        value = None
        if isinstance(fields, dict):
            # prefer an explicit value; fall back to other fields you stored
            value = fields.get("value") or fields.get("chunk_text") or fields.get("label")

        # last resort: try metadata (older patterns)
        if value is None:
            md = best.get("metadata", {}) if isinstance(best, dict) else getattr(best, "metadata", {}) or {}
            if isinstance(md, dict):
                value = md.get("value") or md.get("text") or md.get("chunk_text")
                
        logger.debug("Pinecone resolve: query=%r, best_score=%.4f, value=%r", query_text, best_score, value)
        return ResolveOutput(value=str(value) if value is not None else None)


def main() -> None:
    api_key = os.getenv("PINECONE_API_KEY")
    if not api_key:
        raise RuntimeError("Missing PINECONE_API_KEY in your environment.")

    pc = Pinecone(api_key=api_key)

    resolver_cfg = {
        "target": "surfari.agents.navigation_agent._pinecone_resolver:PineconeManagedEmbedResolver",
        "params": {
            "index": "surfari-index2",
            "namespace": "default",                    
            "top_k": 3,
            "score_threshold": 0.5
        }
    }
    if "value_resolver" in config.CONFIG and config.CONFIG["value_resolver"]:
        if config.CONFIG["value_resolver"].get("target", "") == "surfari.agents.navigation_agent._pinecone_resolver:PineconeManagedEmbedResolver":
            resolver_cfg = config.CONFIG["value_resolver"]
    
    params = resolver_cfg.get("params", {})  
    
    index_name = params.get("index", "surfari-index")
    region = params.get("region", "us-east-1")
    cloud = params.get("cloud", "aws")
    embed_model = params.get("embed_model", "llama-text-embed-v2")
    namespace = params.get("namespace", "default")
    score_threshold = params.get("score_threshold", 0.5)

    if not pc.has_index(index_name):
        logger.info("Index %r not found. Creating with managed embeddings...", index_name)
        pc.create_index_for_model(
            name=index_name,
            cloud=cloud,
            region=region,
            embed={
                "model": embed_model,
                # Our upserts will place text in metadata["chunk_text"]
                "field_map": {"text": "chunk_text"},
            },
        )
        logger.info("Index %r created.", index_name)
    else:
        logger.info("Index %r already exists.", index_name)

    samples = [
        # Source city / airport
        {"id": "src1", "chunk_text": "What is the source city?", "value": "New York (JFK)", "label": "source_city"},
        {"id": "src2", "chunk_text": "Where are we departing from?", "value": "New York (JFK)", "label": "source_city"},
        {"id": "src3", "chunk_text": "Departure airport?", "value": "JFK", "label": "source_airport"},

        # Destination city / airport
        {"id": "dst1", "chunk_text": "What is the destination city?", "value": "San Francisco (SFO)", "label": "destination_city"},
        {"id": "dst2", "chunk_text": "Where are we flying to?", "value": "San Francisco (SFO)", "label": "destination_city"},
        {"id": "dst3", "chunk_text": "Arrival airport?", "value": "SFO", "label": "destination_airport"},

        # Dates
        {"id": "dpt1", "chunk_text": "When are we leaving?", "value": "2025-09-12", "label": "depart_date"},
        {"id": "dpt2", "chunk_text": "Departure date", "value": "2025-09-12", "label": "depart_date"},
        {"id": "ret1", "chunk_text": "When do we come back?", "value": "2025-09-19", "label": "return_date"},
        {"id": "ret2", "chunk_text": "Return date", "value": "2025-09-19", "label": "return_date"},
    ]

    index = pc.Index(index_name)
    stats = index.describe_index_stats()
    if stats["total_vector_count"] == 0:
        logger.info("Upserting %d samples…", len(samples))    
        index.upsert_records(records=samples, namespace=namespace)
    else:
        logger.info("Index %r already contains data.", index_name)
        logger.info("Index stats: %r", stats)
        
    tests = [
        "What is the departure airport?",
        "Where are we flying to?",
        "When is the return flight?",
        "When are we leaving?",
        "What is the destination city?",
    ]
    
    resolver = PineconeManagedEmbedResolver(
        index=index_name,
        embed_model=embed_model,
        namespace=namespace,
        score_threshold=score_threshold,
        top_k=5,
    )
        
    for q in tests:
        out = resolver.resolve(ResolveInput(text=q))
        logger.debug("Query: %s → Result: %s", q, out.value)


if __name__ == "__main__":
    main()
