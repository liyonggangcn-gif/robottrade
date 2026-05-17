import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from src.factors.alpha_engine import AlphaEngine
e = AlphaEngine()
e.update_factors()
print("Factors updated:", e.get_latest_factor_date())
