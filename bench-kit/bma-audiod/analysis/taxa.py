"""Taxonomic group lookup for bird / bird_filtered rows. The table is ui/taxa.json (made by ui/make_taxa.py):
  {"version":1,"explicit_labels":{"dog":{"group":"mammal","non_species":true},...},"genera":{"bird":[genus...],...}}
This module only READS it and never invents a vocabulary: the group strings are the file's (bird, insect, amphibian,
mammal, mechanical) plus "other-bio" for a genus the table does not list. Default location: taxa.json next to this file
(install_task.ps1 copies ui\taxa.json there), override with --taxa.
Missing or corrupt table: group is None and `missing` counts; nothing raises and no record is dropped."""

import json, os

HERE = os.path.dirname(os.path.abspath(__file__))


def default_path():
    return os.path.join(HERE, "taxa.json")


class Taxa:
    def __init__(self, path=None):
        self.path = path or default_path()
        self.missing = 0  # lookups answered with group None because no usable table was loaded
        self.unmapped = 0  # lookups for a genus the table does not list (answered "other-bio")
        self.explicit, self.genus = {}, {}
        self.ok = False
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                d = json.load(f)
            self.explicit = {str(k).lower(): (v["group"], bool(v["non_species"])) for k, v in d["explicit_labels"].items()}
            for grp, gs in d["genera"].items():
                for g in gs:
                    self.genus[str(g).lower()] = grp
            self.ok = True
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            self.explicit, self.genus, self.ok = {}, {}, False

    def lookup(self, scientific, common=None):
        """-> (group or None, non_species or None). non_species None = the table cannot say."""
        if not self.ok:
            self.missing += 1
            return None, None
        sci = (scientific or "").strip().lower()
        if sci in self.explicit:
            return self.explicit[sci]
        g = sci.split()[0] if sci else ""
        if g in self.genus:
            return self.genus[g], False
        self.unmapped += 1
        return "other-bio", False
