"""Petit magasin JSON « clé -> enregistrement » avec écriture atomique (UTF-8).

Chaque étape du pipeline persiste son résultat dans un magasin distinct, ce qui permet de
rejouer une étape sans refaire les précédentes (ni le scraping).
"""
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, ItemsView, KeysView, ValuesView


class StageStore:
    def __init__(self, path):
        self.path = Path(path)
        self._data: Dict[str, Any] = {}
        self._dirty = False
        self.load()

    def load(self) -> None:
        self._data, self._dirty = {}, False
        if not self.path.exists():
            return
        raw = self.path.read_bytes()
        data, error = None, None
        for enc in ("utf-8-sig", "cp1252"):
            try:
                data = json.loads(raw.decode(enc))
                break
            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                error = e
        # Un fichier existant mais illisible ne doit JAMAIS être traité comme vide : le save() suivant
        # écraserait tout l'historique. On s'arrête et on laisse l'utilisateur réparer / restaurer.
        if data is None:
            raise RuntimeError(f"{self.path} est illisible ({error}). Fichier laissé intact : "
                               f"le réparer ou le restaurer avant de relancer.")
        if not isinstance(data, dict):
            raise RuntimeError(f"{self.path} : format inattendu ({type(data).__name__}, dict attendu).")
        records = data.get("records", data) if "records" in data else data
        if not isinstance(records, dict):
            raise RuntimeError(f"{self.path} : 'records' devrait être un dict, pas {type(records).__name__}.")
        self._data = records

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def put(self, key: str, value: Any) -> None:
        self._data[key] = value
        self._dirty = True

    def delete(self, key: str) -> None:
        if key in self._data:
            del self._data[key]
            self._dirty = True

    def touch(self) -> None:
        """À appeler après modification en place d'un enregistrement obtenu par get()."""
        self._dirty = True

    def __contains__(self, key: str) -> bool:
        return key in self._data

    def __len__(self) -> int:
        return len(self._data)

    def keys(self) -> KeysView:
        return self._data.keys()

    def values(self) -> ValuesView:
        return self._data.values()

    def items(self) -> ItemsView:
        return self._data.items()

    def save(self) -> None:
        if not self._dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"version": 1, "records": self._data}, f, ensure_ascii=False, indent=1, default=str)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        self._dirty = False
