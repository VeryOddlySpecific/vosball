"""Static lookup tables for the VOS scoring engine.

Pitch-type maps, personality/injury category encodings, position lists, and
CSV column alternatives. Lifted verbatim from run_vos.py (Phase 1 extraction).
"""
from __future__ import annotations


BASERUNNING_STEAL_COLS = ["StealAbi", "Steal"]

CTRL_COL_ALTERNATIVES = ["Ctrl", "Ctrl_R", "Ctrl_L"]

POT_PITCH_COLUMN_TO_TYPE = {
    "PotFst": "Fastball",
    "PotSnk": "Sinker",
    "PotCutt": "Cutter",
    "PotCrv": "Curve",
    "PotSld": "Slider",
    "PotChg": "Changeup",
    "PotSplt": "Splitter",
    "PotFrk": "Forkball",
    "PotCirChg": "Circle_Change",
    "PotScr": "Screwball",
    "PotKncrv": "Knuckle_Curve",
    "PotKnbl": "Knuckleball",
}

PITCH_SPEED_TIERS = {
    "Fastball": "hard", "Sinker": "hard", "Cutter": "hard",
    "Slider": "breaker", "Curve": "breaker", "Knuckle_Curve": "breaker", "Knuckleball": "breaker",
    "Changeup": "offspeed", "Circle_Change": "offspeed", "Splitter": "offspeed",
    "Forkball": "offspeed", "Screwball": "offspeed",
}

PITCH_BREAK_PLANES = {
    "Fastball": "vertical", "Sinker": "vertical", "Cutter": "horizontal",
    "Slider": "horizontal", "Curve": "vertical", "Knuckle_Curve": "vertical",
    "Knuckleball": "horizontal", "Changeup": "vertical", "Circle_Change": "vertical",
    "Splitter": "vertical", "Forkball": "vertical", "Screwball": "horizontal",
}

PERSONALITY_CSV_TO_CONFIG = {
    "Int": "Intelligence",
    "WrkEthic": "Work_Ethic",
    "Greed": "Greed",
    "Loy": "Loyalty",
    "Lead": "Leadership",
}

PRONE_CATEGORY_TO_NUMERIC = {
    "Wrecked":   90.0,    # ~p95 of non-zero dump values
    "Fragile":   30.0,    # ~median of the non-zero range
    "Normal":    0.0,     # matches the 75% dump baseline
    "Durable":  -10.0,    # slight durability bonus (extrapolates below training range)
    "Iron Man": -20.0,    # extra durability
}

HITTER_POSITIONS = ["C", "1B", "2B", "3B", "SS", "LF", "CF", "RF", "DH"]

LEVEL_LABEL_TO_CONFIG = {"R": "Rookie"}
