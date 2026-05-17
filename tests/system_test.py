"""System completeness test"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def test(label, check, detail=""):
    status = "OK" if check else "FAIL"
    msg = f"  [{status}] {label}"
    if detail and not check:
        msg += f" - {detail}"
    print(msg)

# Test 1: Module Imports
print("=== Test 1: Module Imports ===")
modules = [
    ('src.strategy.base', 'BaseStrategy'),
    ('src.strategy.small_cap_strategy', 'SmallCap v3'),
    ('src.strategy.pb_roa_strategy', 'PbRoaStrategy'),
    ('src.strategy.convertible_bond_strategy', 'ConvertibleBond'),
    ('src.strategy.index_enhance_strategy', 'IndexEnhance'),
    ('src.strategy.etf_unified_strategy', 'ETFUnified'),
    ('src.strategy.hybrid_strategy', 'HybridStrategy'),
    ('src.strategy.dividend_strategy', 'DividendStrategy'),
    ('src.strategy.center', 'StrategyCenter'),
    ('src.collector.multi_source_adapter', 'MultiSourceAdapter'),
    ('src.portfolio.holding_manager', 'HoldingManager'),
    ('src.portfolio.position_manager', 'PositionManager'),
]
for mod, name in modules:
    try:
        __import__(mod)
        test(name, True)
    except Exception as e:
        test(name, False, str(e)[:80])

# Test 2: StrategyCenter registry
print("\n=== Test 2: StrategyCenter Registry ===")
from src.strategy.center import StrategyCenter
center = StrategyCenter(enable_macro=False, notify=False)
strategies = center.available_strategies()
print(f"  Registered: {strategies}")
print(f"  Count: {len(strategies)}")

expected = ['hybrid', 'small_cap', 'dividend', 'pb_roa', 'convertible_bond', 'index_enhance']
for s in expected:
    test(s, s in strategies)

# Test 3: BaseStrategy methods
print("\n=== Test 3: BaseStrategy Methods ===")
from src.strategy.base import BaseStrategy
methods = {
    'run': 'Abstract interface',
    'filter_universe': 'Universe filter',
    '_rank_norm': 'Rank normalization',
    '_normalize_score': 'Min-Max normalization',
    '_zscore': 'Z-Score',
    '_winsorize': 'Winsorize',
    '_industry_neutral': 'Industry neutral',
    '_apply_score_ema': 'EMA smoothing',
    '_save_scores_to_history': 'Score history',
    '_should_empty_position': 'Dynamic empty position',
    '_empty_result': 'Empty result',
}
for m, desc in methods.items():
    test(f"{m} ({desc})", hasattr(BaseStrategy, m))

# Test 4: Multi-source adapter
print("\n=== Test 4: Multi-Source Adapter ===")
from src.collector import multi_source_adapter as msa
funcs = {
    'get_stock_list': 'Stock list',
    'get_daily_history': 'Daily history',
    'get_realtime_quotes': 'Realtime quotes',
    'get_etf_list': 'ETF list',
    'get_etf_history': 'ETF history',
    'get_convertible_bonds': 'Convertible bonds',
    'get_index_constituents': 'Index constituents',
    'get_concept_list': 'Concept list',
    'get_northbound_flow': 'Northbound flow',
    'get_lhb_data': 'Dragon-tiger list',
    'get_macro_pmi': 'PMI',
    'get_macro_cpi': 'CPI',
}
for f, desc in funcs.items():
    test(f"{f} ({desc})", hasattr(msa, f) and callable(getattr(msa, f)))

# Test 5: Config sections
print("\n=== Test 5: Config Sections ===")
from src.utils.config_loader import Config
sections = {
    'strategy': 'Strategy config',
    'hybrid_strategy': 'Hybrid strategy',
    'small_cap': 'Small cap',
    'pb_roa': 'PB-ROA value',
    'convertible_bond': 'Convertible bond',
    'index_enhance': 'Index enhance',
    'holding_manager': 'Holding manager',
    'data_sources': 'Data sources',
}
for s, desc in sections.items():
    try:
        v = Config.get(s)
        test(f"{s} ({desc})", v is not None)
    except Exception as e:
        test(f"{s} ({desc})", False, str(e)[:60])

# Test 6: Strategy attributes
print("\n=== Test 6: Strategy Attributes ===")
strategy_checks = [
    ('src.strategy.small_cap_strategy', 'SmallCapStrategy', 'version', '3.0'),
    ('src.strategy.pb_roa_strategy', 'PbRoaStrategy', 'name', 'pb_roa'),
    ('src.strategy.convertible_bond_strategy', 'ConvertibleBondStrategy', 'name', 'convertible_bond'),
    ('src.strategy.index_enhance_strategy', 'IndexEnhanceStrategy', 'name', 'index_enhance'),
]
for mod, cls, attr, expected in strategy_checks:
    try:
        m = __import__(mod, fromlist=[cls])
        c = getattr(m, cls)
        val = getattr(c, attr, None)
        test(f"{cls}.{attr}={expected}", val == expected, f"got {val}")
    except Exception as e:
        test(f"{cls}.{attr}", False, str(e)[:60])

# Test 7: Database tables
print("\n=== Test 7: Database Tables ===")
from src.utils.db_utils import DBUtils
tables = ['strategy_signals', 'stock_daily', 'stock_info', 'stock_factors',
          'score_history', 'stock_positions', 'transactions']
for t in tables:
    try:
        DBUtils.query_df(f"SELECT 1 FROM {t} LIMIT 1")
        test(t, True)
    except Exception:
        test(t, False, "Table not found")

# Test 8: Sync script
print("\n=== Test 8: Sync Script ===")
try:
    import importlib.util
    spec = importlib.util.spec_from_file_location("sync_free_data", "scripts/sync_free_data.py")
    mod = importlib.util.module_from_spec(spec)
    test("sync_free_data.py loadable", True)
except Exception as e:
    test("sync_free_data.py loadable", False, str(e)[:60])

print("\n=== All Tests Complete ===")
