"""Load the single BND contract used by Supervisor and Operation."""
try:
    from business_quality import *
except ModuleNotFoundError:
    import importlib.util
    from pathlib import Path
    source = Path(__file__).resolve().parents[2] / '08-agent-rag/rag_agent/business_quality.py'
    spec = importlib.util.spec_from_file_location('business_quality', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    globals().update({k:v for k,v in vars(module).items() if not k.startswith('_')})
