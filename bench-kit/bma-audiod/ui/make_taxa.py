#!/usr/bin/env python3
"""Generate ui/taxa.json: the genus -> taxonomic group table the dashboard uses.

    python make_taxa.py <BirdNET_GLOBAL_6K_V2.4_Labels.txt> [out.json]

The labels file ships inside birdnetlib (models/analyzer/); each line is "<scientific>_<common>". Every genus that is not listed
in NON_BIRD below is a bird: BirdNET 2.4 has no other non-bird genus (the non-bird set was read from classify/birdnet_coverage.json
"uncovered" plus the full label list). A label from a future BirdNET version whose genus is in neither list is NOT guessed: the
dashboard files it under 'other-bio' and counts it. Mechanical classes (Engine, Siren ...) and the domestic Dog are explicit
labels, not genera. Stdlib only."""

import json, os, sys

NON_BIRD = {
    "amphibian": [
        "Acris",
        "Anaxyrus",
        "Dryophytes",
        "Eleutherodactylus",
        "Gastrophryne",
        "Hyliola",
        "Incilius",
        "Lithobates",
        "Pseudacris",
        "Scaphiopus",
        "Spea",
    ],
    "insect": [
        "Allonemobius",
        "Amblycorypha",
        "Anaxipha",
        "Apis",
        "Atlanticus",
        "Conocephalus",
        "Cyrtoxipha",
        "Eunemobius",
        "Gryllus",
        "Microcentrum",
        "Miogryllus",
        "Neoconocephalus",
        "Neonemobius",
        "Oecanthus",
        "Orchelimum",
        "Orocharis",
        "Phyllopalpus",
        "Pterophylla",
        "Scudderia",
    ],
    "mammal": ["Alouatta", "Canis", "Odocoileus", "Sciurus", "Tamias", "Tamiasciurus"],
}
EXPLICIT = {  # whole labels (lower case): group and whether the class is a non-species class (hidden by default)
    "dog": ("mammal", False),  # a domestic animal, but a biological sound: shown under Mammal (the owner decided; clips of it are still never stored)
    **{
        k: ("mechanical", True)
        for k in ("engine", "power tools", "siren", "fireworks", "gun", "environmental", "noise", "human vocal", "human non-vocal", "human whistle")
    },
}


def build(labels_path):
    genera = set()
    with open(labels_path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or "_" not in line:
                continue
            sci = line.split("_", 1)[0]
            if sci.split()[0].lower() in ("dog", "engine", "environmental", "fireworks", "gun", "human", "noise", "power", "siren"):
                continue  # one-word / mechanical labels: explicit, not genera
            genera.add(sci.split()[0])
    nb = {g for gs in NON_BIRD.values() for g in gs}
    out = {"bird": sorted(genera - nb)}
    for grp, gs in NON_BIRD.items():
        out[grp] = sorted(gs)
    return {
        "version": 1,
        "source": "birdnetlib BirdNET_GLOBAL_6K_V2.4_Labels.txt",
        "n_labels_read": sum(1 for _ in open(labels_path, encoding="utf-8", errors="replace") if _.strip()),
        "explicit_labels": {k: {"group": g, "non_species": n} for k, (g, n) in sorted(EXPLICIT.items())},
        "genera": {g: [x.lower() for x in v] for g, v in out.items()},
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    dest = sys.argv[2] if len(sys.argv) > 2 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "taxa.json")
    with open(dest, "w", encoding="utf-8", newline="\n") as f:
        json.dump(build(sys.argv[1]), f, indent=0, separators=(",", ":"))
        f.write("\n")
    print("wrote", dest)
