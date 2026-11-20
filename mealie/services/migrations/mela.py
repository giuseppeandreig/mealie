import base64
import io
import json
import re
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from PIL import Image

from mealie.schema.reports.reports import ReportEntryCreate
from mealie.services.scraper import cleaner

from ._migration_base import BaseMigrator
from .utils.migration_alias import MigrationAlias
from .utils.migration_helpers import extract_zip

MELA_EPOCH = datetime(2001, 1, 1, tzinfo=UTC)
"""Mela stores dates as seconds since 2001-01-01 UTC"""

MELA_NUTRITION_LINE = re.compile(r"\*\*(?P<label>.+?)\*\*\s*(?P<value>.*)")
"""A nutrition line in a Mela export, for example `**Calories** 95 calories`"""

MELA_TIME_PART = re.compile(
    r"(?P<amount>\d+(?:\.\d+)?)\s*(?P<unit>d|days?|h|hrs?|hours?|m|mins?|minutes?)\b", re.IGNORECASE
)
"""A part of a time written as text, for example `1 hr` or `30min`"""

MELA_MINUTES_PER_UNIT = {"d": 24 * 60, "h": 60, "m": 1}
"""Minutes in a time unit, by the first letter of the unit"""

MELA_NUTRITION_KEYS = {
    "calories": "calories",
    "carbohydrate": "carbohydrateContent",
    "carbohydrates": "carbohydrateContent",
    "carbs": "carbohydrateContent",
    "cholesterol": "cholesterolContent",
    "fat": "fatContent",
    "total fat": "fatContent",
    "fiber": "fiberContent",
    "fibre": "fiberContent",
    "protein": "proteinContent",
    "saturated fat": "saturatedFatContent",
    "sodium": "sodiumContent",
    "sugar": "sugarContent",
    "sugars": "sugarContent",
    "trans fat": "transFatContent",
    "unsaturated fat": "unsaturatedFatContent",
}


def split_lines(value: str) -> list[str]:
    return [line.strip() for line in value.splitlines() if line.strip()]


def mela_instructions(value: str) -> list[dict]:
    return [{"text": line} for line in split_lines(value)]


def mela_notes(value: str) -> list[dict]:
    return [{"title": "", "text": value}] if value and value.strip() else []


def mela_time(value: str | float) -> str | float:
    """Turns a time written as text, such as `1h 30min`, into minutes. Text that has no known unit is kept as it is."""
    if not isinstance(value, str):
        return value

    parts = list(MELA_TIME_PART.finditer(value))
    if not parts:
        return value

    minutes = sum(float(p["amount"]) * MELA_MINUTES_PER_UNIT[p["unit"][0].lower()] for p in parts)
    return minutes if minutes > 0 else value


def mela_date(value: float) -> str:
    return (MELA_EPOCH + timedelta(seconds=value)).date().isoformat()


def mela_nutrition(value: str) -> dict[str, str]:
    """Reads the nutrition text of a Mela recipe into the keys Mealie uses. Unknown labels are dropped."""
    nutrition: dict[str, str] = {}
    for line in split_lines(value):
        match = MELA_NUTRITION_LINE.match(line)
        if match is None:
            continue

        key = MELA_NUTRITION_KEYS.get(match["label"].strip().lower())
        if key is not None and match["value"].strip():
            nutrition[key] = match["value"].strip()

    return cleaner.clean_nutrition(nutrition)


def mela_recipe_files(directory: Path) -> list[Path]:
    """All recipe files below `directory`, without the hidden files some archive tools add"""
    return sorted(path for path in directory.rglob("*.melarecipe") if not path.name.startswith("."))


def decode_first_image(recipe: dict) -> tuple[bytes, str] | None:
    """Returns the bytes and the file extension of the first image of a recipe, or None if it has no usable image"""
    images = recipe.get("images")
    if not isinstance(images, list) or not images or not isinstance(images[0], str):
        return None

    try:
        data = base64.b64decode(images[0])
        with Image.open(io.BytesIO(data)) as image:
            return (data, f".{image.format.lower()}") if image.format else None
    except Exception:
        return None


class MelaMigrator(BaseMigrator):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

        self.name = "mela"

        self.key_aliases = [
            MigrationAlias(key="name", alias="title"),
            MigrationAlias(key="description", alias="text"),
            MigrationAlias(key="orgURL", alias="link"),
            MigrationAlias(key="recipeYield", alias="yield"),
            MigrationAlias(key="prepTime", alias="prepTime", func=mela_time),
            MigrationAlias(key="performTime", alias="cookTime", func=mela_time),
            MigrationAlias(key="totalTime", alias="totalTime", func=mela_time),
            MigrationAlias(key="recipeIngredient", alias="ingredients", func=split_lines),
            MigrationAlias(key="recipeInstructions", alias="instructions", func=mela_instructions),
            MigrationAlias(key="notes", alias="notes", func=mela_notes),
            MigrationAlias(key="nutrition", alias="nutrition", func=mela_nutrition),
            MigrationAlias(key="dateAdded", alias="date", func=mela_date),
            MigrationAlias(key="recipeCategory", alias="categories"),
        ]

    def _read_recipes(self, source_dir: Path) -> list[dict]:
        recipes: list[dict] = []
        for path in mela_recipe_files(source_dir):
            try:
                recipe = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(recipe, dict):
                    raise ValueError("Recipe file does not contain a JSON object")
            except Exception as e:
                self.logger.exception(e)
                self.report_entries.append(
                    ReportEntryCreate(
                        report_id=self.report_id,
                        success=False,
                        message=f"Failed to import {path.name}",
                        exception=f"{type(e).__name__}: {e}",
                    )
                )
                continue

            if not recipe.get("title"):
                self.logger.info(f"Skipping '{path.name}' because it has no title")
                continue

            recipes.append(recipe)

        return recipes

    def _migrate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            extract_zip(self.archive, tmpdir)

            recipes = self._read_recipes(Path(tmpdir))

            # The images are large and are not part of the recipe model, so they are set aside before cleaning.
            images = [decode_first_image(recipe) if recipe.get("images") else None for recipe in recipes]
            recipe_models = [
                self.clean_recipe_dictionary({k: v for k, v in recipe.items() if k != "images"}) for recipe in recipes
            ]
            results = self.import_recipes_to_database(recipe_models)

            for (slug, recipe_id, status), image, recipe in zip(results, images, recipes, strict=True):
                if not status or not recipe_id:
                    continue

                if image is None:
                    self.logger.info(f"Recipe '{recipe['title']}' has no image")
                    continue

                data, suffix = image
                try:
                    with tempfile.NamedTemporaryFile(suffix=suffix) as temp_file:
                        temp_file.write(data)
                        temp_file.flush()
                        path = Path(temp_file.name)
                        self.import_image(slug, path, recipe_id, extraction_root=path.parent)
                except Exception as e:
                    self.logger.error(f"Failed to import image for {slug}: {e}")
