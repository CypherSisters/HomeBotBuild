"use client";

import { useState } from "react";
import {
  mdiFridge,
  mdiCartOutline,
  mdiPlus,
  mdiDelete,
  mdiCheck,
  mdiClose,
  mdiAlertCircleOutline,
  mdiOpenInNew,
  mdiSilverwareForkKnife,
  mdiBookOpenPageVariant,
} from "@mdi/js";
import SectionTitleLineWithButton from "@/app/_components/Section/TitleLineWithButton";
import CardBox from "@/app/_components/CardBox";
import Icon from "@/app/_components/Icon";
import { useAppData } from "@/app/_lib/hooks/useAppData";
import {
  AppButton,
  AppShell,
  ErrorState,
  LoadingState,
  RowSkeleton,
} from "../_kit";

/* ------------------------------------------------------------------ */
/*  Types                                                              */
/* ------------------------------------------------------------------ */

type PantryItem = {
  id: string;
  name: string;
  emoji: string;
  category: string;
  quantity: number;
  maxQuantity: number;
  unit: string;
  priceEach: number;
  addedDate: string;
};

type ShoppingItem = {
  id: string;
  name: string;
  quantity: number;
  unit: string;
  category: string;
  estimatedPrice: number;
  checked: boolean;
  fromMealPlan: boolean;
};

type MealPlanEntry = {
  id: string;
  day: string; // "monday", "tuesday", etc.
  meal: "breakfast" | "lunch" | "dinner" | "snack";
  dish: string;
  notes?: string;
  recipeId?: string;
};

type Recipe = {
  id: string;
  name: string;
  ingredients: string;
  instructions: string;
  createdAt?: string;
};

type FreshTableData = {
  pantry: PantryItem[];
  shoppingList: ShoppingItem[];
  mealPlan: MealPlanEntry[];
  recipes?: Recipe[];
};

/* ------------------------------------------------------------------ */
/*  Constants                                                          */
/* ------------------------------------------------------------------ */

const CATEGORIES = [
  { label: "All", emoji: "", value: "" },
  { label: "Protein", emoji: "\u{1F969}", value: "\u{1F969} Protein" },
  { label: "Produce", emoji: "\u{1F96C}", value: "\u{1F96C} Produce" },
  { label: "Dairy", emoji: "\u{1F9C0}", value: "\u{1F9C0} Dairy" },
  { label: "Grains", emoji: "\u{1F35E}", value: "\u{1F35E} Grains" },
  { label: "Pantry Staples", emoji: "\u{1F96B}", value: "\u{1F96B} Pantry Staples" },
  { label: "Frozen", emoji: "\u{2744}\u{FE0F}", value: "\u{2744}\u{FE0F} Frozen" },
  { label: "Beverage", emoji: "\u{1F964}", value: "\u{1F964} Beverage" },
  { label: "Snacks", emoji: "\u{1F36C}", value: "\u{1F36C} Snacks" },
];

const UNITS = ["lbs", "oz", "cups", "pcs", "bags", "cans"];

/* ------------------------------------------------------------------ */
/*  Page                                                               */
/* ------------------------------------------------------------------ */

export default function FreshTablePage() {
  const { data, loading, error, runAction, update, reload } =
    useAppData<FreshTableData>("freshtable");

  const [activeTab, setActiveTab] = useState<"pantry" | "shopping" | "mealplan" | "recipes">("pantry");
  const [filterCat, setFilterCat] = useState("");

  /* Pantry add form */
  const [pName, setPName] = useState("");
  const [pQty, setPQty] = useState(1);
  const [pUnit, setPUnit] = useState("pcs");
  const [pPrice, setPPrice] = useState(0);
  const [pCategory, setPCategory] = useState(CATEGORIES[1].value);
  const [showPantryForm, setShowPantryForm] = useState(false);

  /* Shopping add form */
  const [sName, setSName] = useState("");
  const [sQty, setSQty] = useState(1);
  const [sPrice, setSPrice] = useState(0);
  const [sCategory, setSCategory] = useState(CATEGORIES[1].value);
  const [showShoppingForm, setShowShoppingForm] = useState(false);

  /* ---- derived ---- */
  const pantry = data?.pantry ?? [];
  const shopping = data?.shoppingList ?? [];
  const filteredPantry = filterCat
    ? pantry.filter((i) => i.category === filterCat)
    : pantry;
  const shoppingTotal = shopping.reduce(
    (sum, i) => sum + i.estimatedPrice * i.quantity,
    0,
  );

  /* ---- handlers ---- */
  async function addPantryItem() {
    if (!pName.trim()) return;
    await runAction("fresh_add_pantry", {
      name: pName.trim(),
      quantity: pQty,
      unit: pUnit,
      category: pCategory,
      price: pPrice,
    });
    setPName("");
    setPQty(1);
    setPPrice(0);
    setShowPantryForm(false);
  }

  async function addShoppingItem() {
    if (!sName.trim()) return;
    await runAction("fresh_add_shopping", {
      name: sName.trim(),
      quantity: sQty,
      est_price: sPrice,
      category: sCategory,
    });
    setSName("");
    setSQty(1);
    setSPrice(0);
    setShowShoppingForm(false);
  }

  async function toggleChecked(id: string) {
    if (!data) return;
    const updated = {
      ...data,
      shoppingList: data.shoppingList.map((i) =>
        i.id === id ? { ...i, checked: !i.checked } : i,
      ),
    };
    await update(updated);
  }

  async function clearChecked() {
    if (!data) return;
    const updated = {
      ...data,
      shoppingList: data.shoppingList.filter((i) => !i.checked),
    };
    await update(updated);
  }

  /* ---- loading / error ----
     Both keep FreshTable's own shape: a kitchen with nothing in it is still
     a kitchen, and neither renders the shared backend sentence. */
  const shellActions = (
    <div className="puff-taprow flex gap-2">
      <AppButton
        icon={mdiOpenInNew}
        label="Open Kitchen"
        tone="ok"
        small
        roundedFull
        href="/dashboard/apps/freshtable/manage"
      />
    </div>
  );

  if (loading) {
    return (
      <AppShell title="FreshTable" hint="Pantry and shopping" actions={shellActions}>
        <LoadingState skeleton={<RowSkeleton rows={6} />} line="Loading the kitchen" />
      </AppShell>
    );
  }

  if (error) {
    return (
      <AppShell title="FreshTable" hint="Pantry and shopping" actions={shellActions}>
        <ErrorState
          skeleton={<RowSkeleton rows={6} />}
          line="The pantry and list are not loading right now."
          detail={error}
          onRetry={reload}
        />
      </AppShell>
    );
  }

  return (
    <AppShell title="FreshTable" hint="Pantry and shopping" actions={shellActions}>
      {/* ---- Tabs ---- */}
      <div className="puff-taprow flex flex-wrap gap-2">
        <AppButton
          icon={mdiFridge}
          label="Pantry"
          tone="ok"
              selected={activeTab === "pantry"}
          roundedFull
          small
          onClick={() => setActiveTab("pantry")}
        />
        <AppButton
          icon={mdiCartOutline}
          label="Shopping List"
          tone="ok"
              selected={activeTab === "shopping"}
          roundedFull
          small
          onClick={() => setActiveTab("shopping")}
        />
        <AppButton
          icon={mdiSilverwareForkKnife}
          label="Meal Plan"
          tone="ok"
              selected={activeTab === "mealplan"}
          roundedFull
          small
          onClick={() => setActiveTab("mealplan")}
        />
        <AppButton
          icon={mdiBookOpenPageVariant}
          label="Recipes"
          tone="ok"
              selected={activeTab === "recipes"}
          roundedFull
          small
          onClick={() => setActiveTab("recipes")}
        />
      </div>

      {/* ============================================================== */}
      {/*  PANTRY TAB                                                     */}
      {/* ============================================================== */}
      {activeTab === "pantry" && (
        <>
          <SectionTitleLineWithButton icon={mdiFridge} title="Pantry">
            <AppButton
              icon={mdiPlus}
              label="Add Item"
              tone="ok"
              small
              roundedFull
              onClick={() => setShowPantryForm((v) => !v)}
            />
          </SectionTitleLineWithButton>

          {/* Category Filters */}
          <div className="mb-4 flex flex-wrap gap-2">
            {CATEGORIES.map((c) => (
              <button
                key={c.label}
                onClick={() => setFilterCat(c.value)}
                className={`puff-pill ${filterCat === c.value ? "puff-pill-on" : ""}`}
                style={
                  filterCat === c.value
                    ? { backgroundColor: "var(--mint)", borderColor: "var(--mint)" }
                    : undefined
                }
              >
                {c.emoji ? `${c.emoji} ${c.label}` : c.label}
              </button>
            ))}
          </div>

          {/* Add Pantry Form */}
          {showPantryForm && (
            <CardBox className="mb-6">
              <h3 className="mb-3 text-lg font-semibold text-[var(--text)]">
                Add Pantry Item
              </h3>
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-5">
                <input
                  type="text"
                  placeholder="Item name"
                  value={pName}
                  onChange={(e) => setPName(e.target.value)}
                  className="rounded-lg border border-[var(--line)] bg-white/80 px-3 py-2 text-sm focus:border-[var(--mint)] focus:outline-none focus:ring-2 focus:ring-[color-mix(in_srgb,var(--mint)_30%,transparent)]"
                />
                <input
                  type="number"
                  placeholder="Qty"
                  min={1}
                  value={pQty}
                  onChange={(e) => setPQty(Number(e.target.value))}
                  className="rounded-lg border border-[var(--line)] bg-white/80 px-3 py-2 text-sm focus:border-[var(--mint)] focus:outline-none focus:ring-2 focus:ring-[color-mix(in_srgb,var(--mint)_30%,transparent)]"
                />
                <select
                  value={pUnit}
                  onChange={(e) => setPUnit(e.target.value)}
                  className="rounded-lg border border-[var(--line)] bg-white/80 px-3 py-2 text-sm focus:border-[var(--mint)] focus:outline-none focus:ring-2 focus:ring-[color-mix(in_srgb,var(--mint)_30%,transparent)]"
                >
                  {UNITS.map((u) => (
                    <option key={u} value={u}>
                      {u}
                    </option>
                  ))}
                </select>
                <input
                  type="number"
                  placeholder="Price ($)"
                  min={0}
                  step={0.01}
                  value={pPrice || ""}
                  onChange={(e) => setPPrice(Number(e.target.value))}
                  className="rounded-lg border border-[var(--line)] bg-white/80 px-3 py-2 text-sm focus:border-[var(--mint)] focus:outline-none focus:ring-2 focus:ring-[color-mix(in_srgb,var(--mint)_30%,transparent)]"
                />
                <select
                  value={pCategory}
                  onChange={(e) => setPCategory(e.target.value)}
                  className="rounded-lg border border-[var(--line)] bg-white/80 px-3 py-2 text-sm focus:border-[var(--mint)] focus:outline-none focus:ring-2 focus:ring-[color-mix(in_srgb,var(--mint)_30%,transparent)]"
                >
                  {CATEGORIES.filter((c) => c.value).map((c) => (
                    <option key={c.value} value={c.value}>
                      {c.emoji} {c.label}
                    </option>
                  ))}
                </select>
              </div>
              <div className="mt-3 flex gap-2">
                <AppButton
                  icon={mdiCheck}
                  label="Add"
                  tone="ok"
                  small
                  roundedFull
                  onClick={addPantryItem}
                />
                <AppButton
                  icon={mdiClose}
                  label="Cancel"
                  tone="quiet"
                  small
                  roundedFull
                  onClick={() => setShowPantryForm(false)}
                />
              </div>
            </CardBox>
          )}

          {/* Pantry Grid */}
          {filteredPantry.length === 0 ? (
            <CardBox className="text-center text-[var(--text3)]">
              <div className="py-8">
                <Icon
                  path={mdiFridge}
                  size="48"
                  className="mx-auto mb-2 opacity-30"
                />
                <p>
                  No items in pantry
                  {filterCat ? " for this category" : ""}. Add some!
                </p>
              </div>
            </CardBox>
          ) : (
            <div className="puff-grid-wide">
              {filteredPantry.map((item) => (
                <CardBox key={item.id} isHoverable className="relative">
                  {/* Low stock warning */}
                  {item.quantity < 2 && (
                    <div className="absolute right-3 top-3 flex items-center gap-1 rounded-full bg-[color-mix(in_srgb,var(--lemon)_16%,transparent)] px-2 py-0.5 text-xs font-medium text-[var(--lemon)]">
                      <Icon path={mdiAlertCircleOutline} size="12" />
                      Low
                    </div>
                  )}
                  <div className="flex items-start gap-3">
                    <div className="flex h-12 w-12 flex-shrink-0 items-center justify-center rounded-xl bg-[color-mix(in_srgb,var(--mint)_10%,transparent)] text-2xl">
                      {item.emoji}
                    </div>
                    <div className="min-w-0 flex-1">
                      <h4 className="font-semibold text-[var(--text)]">
                        {item.name}
                      </h4>
                      <p className="text-sm text-[var(--text2)]">
                        {item.quantity} {item.unit}
                        {item.priceEach > 0 && (
                          <span className="ml-2 text-[var(--mint)]">
                            ${item.priceEach.toFixed(2)}
                          </span>
                        )}
                      </p>
                      <span className="mt-1 inline-block rounded-full bg-[color-mix(in_srgb,var(--mint)_10%,transparent)] px-2 py-0.5 text-xs font-medium text-[var(--mint)]">
                        {item.category}
                      </span>
                    </div>
                  </div>
                  {/* Quantity bar */}
                  {item.maxQuantity > 0 && (
                    <div className="mt-3">
                      <div className="h-1.5 w-full overflow-hidden rounded-full bg-[var(--chip)]">
                        <div
                          className={`h-full rounded-full transition-all ${
                            item.quantity / item.maxQuantity < 0.33
                              ? "bg-[var(--puff-red)]"
                              : item.quantity / item.maxQuantity < 0.66
                                ? "bg-[var(--lemon)]"
                                : "bg-[var(--mint)]"
                          }`}
                          style={{
                            width: `${Math.min(100, (item.quantity / item.maxQuantity) * 100)}%`,
                          }}
                        />
                      </div>
                    </div>
                  )}
                </CardBox>
              ))}
            </div>
          )}
        </>
      )}

      {/* ============================================================== */}
      {/*  SHOPPING LIST TAB                                              */}
      {/* ============================================================== */}
      {activeTab === "shopping" && (
        <>
          <SectionTitleLineWithButton icon={mdiCartOutline} title="Shopping List">
            <div className="flex gap-2">
              {shopping.some((i) => i.checked) && (
                <AppButton
                  icon={mdiDelete}
                  label="Clear Checked"
                  tone="danger"
                  small
                  roundedFull
                  onClick={clearChecked}
                />
              )}
              <AppButton
                icon={mdiPlus}
                label="Add Item"
                tone="ok"
                small
                roundedFull
                onClick={() => setShowShoppingForm((v) => !v)}
              />
            </div>
          </SectionTitleLineWithButton>

          {/* Add Shopping Form */}
          {showShoppingForm && (
            <CardBox className="mb-6">
              <h3 className="mb-3 text-lg font-semibold text-[var(--text)]">
                Add to Shopping List
              </h3>
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
                <input
                  type="text"
                  placeholder="Item name"
                  value={sName}
                  onChange={(e) => setSName(e.target.value)}
                  className="rounded-lg border border-[var(--line)] bg-white/80 px-3 py-2 text-sm focus:border-[var(--mint)] focus:outline-none focus:ring-2 focus:ring-[color-mix(in_srgb,var(--mint)_30%,transparent)]"
                />
                <input
                  type="number"
                  placeholder="Qty"
                  min={1}
                  value={sQty}
                  onChange={(e) => setSQty(Number(e.target.value))}
                  className="rounded-lg border border-[var(--line)] bg-white/80 px-3 py-2 text-sm focus:border-[var(--mint)] focus:outline-none focus:ring-2 focus:ring-[color-mix(in_srgb,var(--mint)_30%,transparent)]"
                />
                <input
                  type="number"
                  placeholder="Est. Price ($)"
                  min={0}
                  step={0.01}
                  value={sPrice || ""}
                  onChange={(e) => setSPrice(Number(e.target.value))}
                  className="rounded-lg border border-[var(--line)] bg-white/80 px-3 py-2 text-sm focus:border-[var(--mint)] focus:outline-none focus:ring-2 focus:ring-[color-mix(in_srgb,var(--mint)_30%,transparent)]"
                />
                <select
                  value={sCategory}
                  onChange={(e) => setSCategory(e.target.value)}
                  className="rounded-lg border border-[var(--line)] bg-white/80 px-3 py-2 text-sm focus:border-[var(--mint)] focus:outline-none focus:ring-2 focus:ring-[color-mix(in_srgb,var(--mint)_30%,transparent)]"
                >
                  {CATEGORIES.filter((c) => c.value).map((c) => (
                    <option key={c.value} value={c.value}>
                      {c.emoji} {c.label}
                    </option>
                  ))}
                </select>
              </div>
              <div className="mt-3 flex gap-2">
                <AppButton
                  icon={mdiCheck}
                  label="Add"
                  tone="ok"
                  small
                  roundedFull
                  onClick={addShoppingItem}
                />
                <AppButton
                  icon={mdiClose}
                  label="Cancel"
                  tone="quiet"
                  small
                  roundedFull
                  onClick={() => setShowShoppingForm(false)}
                />
              </div>
            </CardBox>
          )}

          {/* Shopping Items */}
          {shopping.length === 0 ? (
            <CardBox className="text-center text-[var(--text3)]">
              <div className="py-8">
                <Icon
                  path={mdiCartOutline}
                  size="48"
                  className="mx-auto mb-2 opacity-30"
                />
                <p>Shopping list is empty. Add items!</p>
              </div>
            </CardBox>
          ) : (
            <CardBox>
              <div className="divide-y divide-[var(--line)]">
                {shopping.map((item) => (
                  <div
                    key={item.id}
                    className={`flex items-center gap-3 py-3 transition ${
                      item.checked ? "opacity-50" : ""
                    }`}
                  >
                    <button
                      onClick={() => toggleChecked(item.id)}
                      className={`flex h-6 w-6 flex-shrink-0 items-center justify-center rounded-md border-2 transition ${
                        item.checked
                          ? "border-[var(--mint)] bg-[var(--mint)] puff-on-accent"
                          : "border-[var(--line)] hover:border-[var(--mint)]"
                      }`}
                    >
                      {item.checked && <Icon path={mdiCheck} size="14" />}
                    </button>
                    <div className="min-w-0 flex-1">
                      <span
                        className={`font-medium text-[var(--text)] ${
                          item.checked ? "line-through" : ""
                        }`}
                      >
                        {item.name}
                      </span>
                      <span className="ml-2 text-sm text-[var(--text3)]">
                        x{item.quantity}
                      </span>
                      {item.fromMealPlan && (
                        <span className="ml-2 rounded-full bg-[color-mix(in_srgb,var(--sky)_16%,transparent)] px-2 py-0.5 text-xs text-[var(--sky)]">
                          Meal Plan
                        </span>
                      )}
                    </div>
                    <span className="whitespace-nowrap text-sm font-semibold text-[var(--mint)]">
                      ${(item.estimatedPrice * item.quantity).toFixed(2)}
                    </span>
                  </div>
                ))}
              </div>

              {/* Total */}
              <div className="mt-4 flex items-center justify-between border-t border-[var(--line)] pt-4">
                <span className="text-lg font-bold text-[var(--text)]">
                  Estimated Total
                </span>
                <span className="text-xl font-bold text-[var(--mint)]">
                  ${shoppingTotal.toFixed(2)}
                </span>
              </div>
            </CardBox>
          )}
        </>
      )}

      {/* ============================================================== */}
      {/*  MEAL PLAN TAB                                                  */}
      {/* ============================================================== */}
      {activeTab === "mealplan" && (
        <MealPlanTab data={data} update={update} />
      )}

      {/* ============================================================== */}
      {/*  RECIPES TAB                                                    */}
      {/* ============================================================== */}
      {activeTab === "recipes" && (
        <RecipesTab data={data} update={update} />
      )}
    </AppShell>
  );
}

/* ------------------------------------------------------------------ */
/*  Meal Plan tab                                                      */
/* ------------------------------------------------------------------ */

const DAYS = [
  { key: "monday",    label: "Mon" },
  { key: "tuesday",   label: "Tue" },
  { key: "wednesday", label: "Wed" },
  { key: "thursday",  label: "Thu" },
  { key: "friday",    label: "Fri" },
  { key: "saturday",  label: "Sat" },
  { key: "sunday",    label: "Sun" },
];
const MEAL_TYPES: Array<MealPlanEntry["meal"]> = ["breakfast", "lunch", "dinner", "snack"];

function MealPlanTab({
  data,
  update,
}: {
  data: FreshTableData | null;
  update: (d: FreshTableData) => Promise<void>;
}) {
  const mealPlan = (data?.mealPlan ?? []) as MealPlanEntry[];

  const setEntry = async (
    day: string,
    meal: MealPlanEntry["meal"],
    dish: string,
  ) => {
    if (!data) return;
    const filtered = mealPlan.filter((e) => !(e.day === day && e.meal === meal));
    if (dish.trim()) {
      filtered.push({
        id: `mp_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`,
        day,
        meal,
        dish: dish.trim(),
      });
    }
    await update({ ...data, mealPlan: filtered });
  };

  const getDish = (day: string, meal: string): string => {
    const entry = mealPlan.find((e) => e.day === day && e.meal === meal);
    return entry?.dish || "";
  };

  return (
    <>
      <SectionTitleLineWithButton
        icon={mdiSilverwareForkKnife}
        title="Weekly Meal Plan"
      />
      <CardBox>
        <div className="overflow-x-auto p-4">
          <table className="w-full min-w-[700px] border-collapse text-sm">
            <thead>
              <tr>
                <th className="w-24 border-b border-[var(--line)] pb-2 text-left font-semibold text-[var(--text)]">
                  Day
                </th>
                {MEAL_TYPES.map((m) => (
                  <th
                    key={m}
                    className="border-b border-[var(--line)] pb-2 text-left font-semibold capitalize text-[var(--text)]"
                  >
                    {m}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {DAYS.map((day) => (
                <tr key={day.key}>
                  <td className="border-b border-[var(--line)] py-2 pr-2 font-semibold text-[var(--text)]">
                    {day.label}
                  </td>
                  {MEAL_TYPES.map((meal) => (
                    <td
                      key={meal}
                      className="border-b border-[var(--line)] py-2 pr-2"
                    >
                      <input
                        type="text"
                        defaultValue={getDish(day.key, meal)}
                        onBlur={(e) => setEntry(day.key, meal, e.target.value)}
                        placeholder="—"
                        className="w-full rounded-md border border-[var(--line)] bg-white/80 px-2 py-1 text-sm text-[var(--text)] focus:border-[var(--mint)] focus:outline-none focus:ring-1 focus:ring-[color-mix(in_srgb,var(--mint)_30%,transparent)]"
                      />
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
          <p className="mt-3 text-xs text-[var(--text2)]">
            Type a dish and click outside the field to save. Or just ask PuffPAI:
            &quot;plan dinners for this week&quot;.
          </p>
        </div>
      </CardBox>
    </>
  );
}

/* ------------------------------------------------------------------ */
/*  Recipes tab                                                        */
/* ------------------------------------------------------------------ */

function RecipesTab({
  data,
  update,
}: {
  data: FreshTableData | null;
  update: (d: FreshTableData) => Promise<void>;
}) {
  const recipes = data?.recipes ?? [];
  const [showForm, setShowForm] = useState(false);
  const [name, setName] = useState("");
  const [ingredients, setIngredients] = useState("");
  const [instructions, setInstructions] = useState("");

  const addRecipe = async () => {
    if (!name.trim() || !ingredients.trim() || !data) return;
    const newRecipe: Recipe = {
      id: `r_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`,
      name: name.trim(),
      ingredients: ingredients.trim(),
      instructions: instructions.trim(),
      createdAt: new Date().toISOString(),
    };
    await update({
      ...data,
      recipes: [...recipes, newRecipe],
    });
    setName("");
    setIngredients("");
    setInstructions("");
    setShowForm(false);
  };

  const deleteRecipe = async (id: string) => {
    if (!data) return;
    await update({
      ...data,
      recipes: recipes.filter((r) => r.id !== id),
    });
  };

  return (
    <>
      <SectionTitleLineWithButton icon={mdiBookOpenPageVariant} title="Recipes">
        <AppButton
          icon={mdiPlus}
          label={showForm ? "Cancel" : "Add Recipe"}
          tone="ok"
          small
          roundedFull
          onClick={() => setShowForm((v) => !v)}
        />
      </SectionTitleLineWithButton>

      {showForm && (
        <CardBox className="mb-4">
          <div className="space-y-3 p-4">
            <input
              type="text"
              placeholder="Recipe name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              className="w-full rounded-md border border-[var(--line)] bg-white/80 px-3 py-2 text-sm"
            />
            <textarea
              placeholder="Ingredients (comma-separated)"
              value={ingredients}
              onChange={(e) => setIngredients(e.target.value)}
              rows={3}
              className="w-full rounded-md border border-[var(--line)] bg-white/80 px-3 py-2 text-sm"
            />
            <textarea
              placeholder="Instructions"
              value={instructions}
              onChange={(e) => setInstructions(e.target.value)}
              rows={4}
              className="w-full rounded-md border border-[var(--line)] bg-white/80 px-3 py-2 text-sm"
            />
            <AppButton label="Save Recipe" tone="ok" onClick={addRecipe} />
          </div>
        </CardBox>
      )}

      {recipes.length === 0 ? (
        <CardBox>
          <div className="p-8 text-center text-[var(--text2)]">
            <p>No recipes yet.</p>
            <p className="mt-1 text-xs">
              Ask PuffPAI: &quot;create a recipe for chicken stir fry&quot;
            </p>
          </div>
        </CardBox>
      ) : (
        <div className="puff-grid-2">
          {recipes.map((r) => (
            <CardBox key={r.id}>
              <div className="p-4">
                <div className="mb-2 flex items-start justify-between gap-2">
                  <h3 className="text-base font-bold text-[var(--text)]">
                    {r.name}
                  </h3>
                  <button
                    onClick={() => deleteRecipe(r.id)}
                    className="text-[var(--text3)] hover:text-[var(--puff-red)]"
                    aria-label="Delete recipe"
                  >
                    <Icon path={mdiDelete} size="16" />
                  </button>
                </div>
                <p className="mb-2 text-xs uppercase tracking-wide text-[var(--text2)]">
                  Ingredients
                </p>
                <p className="mb-3 text-sm text-[var(--text)]">
                  {r.ingredients}
                </p>
                {r.instructions && (
                  <>
                    <p className="mb-2 text-xs uppercase tracking-wide text-[var(--text2)]">
                      Instructions
                    </p>
                    <p className="text-sm text-[var(--text)]">
                      {r.instructions}
                    </p>
                  </>
                )}
              </div>
            </CardBox>
          ))}
        </div>
      )}
    </>
  );
}
