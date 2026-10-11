import json
import os
from datetime import datetime

from puffpai.statedir import resolve_runtime_dir

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: Test/ops override: set to an absolute path to pin the inventory file.
#: ``None`` means "resolve" — the repo root historically, or
#: ``$PUFFPAI_DATA_DIR/emergency_inventory.json`` when the env var is set. On a
#: hardened unit the repo is read-only, so the save below would fail there.
_INVENTORY_FILE = None


def _inventory_path() -> str:
    """Absolute path of the inventory file, resolved per instance.

    Resolved here rather than at import because :mod:`puffpai.statedir` reaches
    ``puffpai.config`` (and thus the secrets provider) at call time.
    """
    if _INVENTORY_FILE:
        return _INVENTORY_FILE
    return os.path.join(resolve_runtime_dir(_REPO_ROOT), "emergency_inventory.json")


def _default_location() -> str:
    """``config.location`` for a fresh inventory; '' when config is unavailable."""
    try:
        from puffpai.household_geo import household_location
        return household_location()
    except Exception:
        return ""


class EmergencyPrep:
    def __init__(self):
        self.inventory_file = _inventory_path()
        self.inventory = self.load_inventory()
    
    def load_inventory(self):
        if os.path.exists(self.inventory_file):
            with open(self.inventory_file, 'r') as f:
                return json.load(f)
        return {
            "supplies": {},
            "family_size": 4,
            # The household's configured place, not a hardcoded city: a fresh
            # inventory must plan for where this house is.
            "location": _default_location(),
            "last_updated": None
        }
    
    def effective_location(self) -> str:
        """The place this household's prep is about.

        The stored value wins (``prep_set_location`` is the owner's explicit
        choice); with none stored, the configured place is used.
        """
        stored = (self.inventory.get("location") or "").strip()
        configured = _default_location()
        if not stored:
            return configured
        return stored

    def save_inventory(self):
        # The resolved dir may not exist yet on a fresh $PUFFPAI_DATA_DIR.
        parent = os.path.dirname(self.inventory_file)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(self.inventory_file, 'w') as f:
            json.dump(self.inventory, f, indent=2)
    
    def get_inventory_summary(self):
        summary = f"Emergency Supplies Inventory\n"
        summary += f"Family size: {self.inventory['family_size']}\n"
        summary += f"Location: {self.inventory['location']}\n\n"
        
        for category, items in self.inventory["supplies"].items():
            summary += f"{category.title()}:\n"
            for item in items:
                summary += f"  - {item['quantity']} {item['unit']} of {item['item']}\n"
        
        return summary
    
    def calculate_days_of_supplies(self):
        water = self.get_total_quantity("water", "gallons")
        food = self.get_total_quantity("food", "servings")
        
        family_size = self.inventory["family_size"]
        
        water_days = water / (1 * family_size) if water > 0 else 0
        food_days = food / (3 * family_size) if food > 0 else 0
        
        return {
            "water_days": round(water_days, 1),
            "food_days": round(food_days, 1),
            "limiting_factor": "water" if water_days < food_days else "food"
        }
    
    def get_total_quantity(self, category, unit):
        if category not in self.inventory["supplies"]:
            return 0
        
        total = 0
        for item in self.inventory["supplies"][category]:
            if item["unit"] == unit:
                total += item["quantity"]
        
        return total
    
    def get_recommended_supplies(self):
        recommended = {
            "water": {"quantity": 14, "unit": "gallons", "reason": "1 gal/person/day for 14 days"},
            "food": {"quantity": 168, "unit": "servings", "reason": "3 meals/person/day for 14 days"},
            "medical": {"quantity": 1, "unit": "kit", "reason": "Comprehensive first aid kit"},
            "power": {"quantity": 1, "unit": "generator", "reason": "Backup power source"},
            "lighting": {"quantity": 4, "unit": "flashlights", "reason": "1 per person"},
            "batteries": {"quantity": 24, "unit": "AA batteries", "reason": "For flashlights and radio"},
            "radio": {"quantity": 1, "unit": "emergency radio", "reason": "NOAA weather alerts"},
            "sanitation": {"quantity": 1, "unit": "kit", "reason": "Waste bags, sanitizer, toilet paper"}
        }
        
        gaps = {}
        for category, rec in recommended.items():
            current = self.get_total_quantity(category, rec["unit"])
            needed = rec["quantity"] * (self.inventory["family_size"] / 4)
            
            if current < needed:
                gaps[category] = {
                    "current": current,
                    "recommended": round(needed, 1),
                    "needed": round(needed - current, 1),
                    "unit": rec["unit"],
                    "reason": rec["reason"]
                }
        
        return gaps
    
    def get_expiring_items(self, days_ahead=30):
        expiring = []
        today = datetime.now()
        
        for category, items in self.inventory["supplies"].items():
            for item in items:
                if item.get("expiration"):
                    exp_date = datetime.fromisoformat(item["expiration"])
                    days_until = (exp_date - today).days
                    
                    if 0 <= days_until <= days_ahead:
                        expiring.append({
                            "category": category,
                            "item": item["item"],
                            "quantity": item["quantity"],
                            "unit": item["unit"],
                            "expiration": item["expiration"],
                            "days_until": days_until
                        })
        
        return sorted(expiring, key=lambda x: x["days_until"])
    
    def add_supply(self, category, item, quantity, unit, expiration=None):
        if category not in self.inventory["supplies"]:
            self.inventory["supplies"][category] = []
        
        supply_item = {
            "item": item,
            "quantity": quantity,
            "unit": unit,
            "added": datetime.now().isoformat()
        }
        
        if expiration:
            supply_item["expiration"] = expiration
        
        self.inventory["supplies"][category].append(supply_item)
        
        self.inventory["last_updated"] = datetime.now().isoformat()
        self.save_inventory()
        
        exp_msg = f" (expires {expiration})" if expiration else ""
        return f"Added {quantity} {unit} of {item} to {category} supplies{exp_msg}"