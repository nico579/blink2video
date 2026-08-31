"""Petits helpers partagés par les recettes de construction PyInstaller."""

from __future__ import annotations

from pathlib import Path


def ecrire_version_info(
    version: str,
    chemin: Path,
    *,
    description: str = "blink2video - enregistreur video pour cameras Blink",
    internal_name: str = "blink2video",
    original_filename: str = "blink2video.exe",
    product_name: str = "blink2video",
) -> Path:
    """Génère la ressource VERSIONINFO Windows utilisée par PyInstaller.

    Un PE sans éditeur, description ni copyright ressemble davantage aux
    échantillons malveillants pour certains antivirus heuristiques. Centraliser
    ces champs évite aussi que le bundle principal et le testeur XR dépendent
    d'un fichier généré par une construction antérieure.
    """
    parties = (version.split(".") + ["0", "0", "0"])[:3]
    tuple_version = tuple(int(partie) for partie in parties) + (0,)
    chemin = Path(chemin)
    chemin.write_text(f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={tuple_version},
    prodvers={tuple_version},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo(
      [StringTable(
        u'040904B0',
        [StringStruct(u'CompanyName', u'nico579'),
         StringStruct(u'FileDescription', u'{description}'),
         StringStruct(u'FileVersion', u'{version}'),
         StringStruct(u'InternalName', u'{internal_name}'),
         StringStruct(u'LegalCopyright', u'GPLv3 - nico579'),
         StringStruct(u'OriginalFilename', u'{original_filename}'),
         StringStruct(u'ProductName', u'{product_name}'),
         StringStruct(u'ProductVersion', u'{version}')])
      ]),
    VarFileInfo([VarStruct(u'Translation', [1033, 1200])])
  ]
)
""", encoding="utf-8")
    return chemin
