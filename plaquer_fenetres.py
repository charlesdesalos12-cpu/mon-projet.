"""
Plaquer toutes les fenêtres contre la façade de leur immeuble (Blender 2.8+ / 3.x / 4.x / 5.x).

Utilisation :
  1. Ouvrir le fichier .blend.
  2. Onglet "Scripting" > "Ouvrir" > choisir ce fichier (ou coller le contenu).
  3. Adapter la section CONFIGURATION ci-dessous si besoin.
  4. Cliquer sur "Run Script" (Alt+P). Ctrl+Z annule tout d'un coup.

Pour chaque fenêtre, le script :
  - trouve le point le plus proche sur la surface de l'immeuble le plus proche,
  - (optionnel) tourne la fenêtre pour qu'elle soit parallèle au mur,
  - la déplace le long de la normale du mur pour que sa face arrière
    touche exactement le mur (plus un petit décalage anti-scintillement).
"""

import bpy
import bmesh
from mathutils import Matrix, Vector
from mathutils.bvhtree import BVHTree

# ----------------------------------------------------------------------------
# CONFIGURATION
# ----------------------------------------------------------------------------

# Si True et que des objets sont sélectionnés : seules les fenêtres
# sélectionnées sont traitées. Sinon, on détecte les fenêtres par leur nom.
UTILISER_SELECTION = True

# Mots-clés (insensibles à la casse) pour reconnaître les objets par leur nom.
MOTS_CLES_FENETRE = ("fenetre", "fenêtre", "window", "vitre", "fen")
MOTS_CLES_IMMEUBLE = ("immeuble", "building", "batiment", "bâtiment", "facade", "façade", "mur")

# Noms de collections (facultatif). Si renseignés, ils priment sur les mots-clés.
COLLECTION_FENETRES = ""   # ex. "Fenetres"
COLLECTION_IMMEUBLES = ""  # ex. "Immeubles"

# Tourner la fenêtre pour qu'elle soit bien parallèle au mur.
ALIGNER_ROTATION = True

# Petit écart entre la fenêtre et le mur (en unités Blender) pour éviter le
# scintillement (z-fighting). Mettre 0.0 pour un contact parfait.
DECALAGE = 0.002

# Une fenêtre à plus de cette distance de tout immeuble est ignorée.
DISTANCE_MAX = 5.0

# ----------------------------------------------------------------------------


def _correspond(nom, mots):
    nom = nom.lower()
    return any(m.lower() in nom for m in mots)


def _objets_collection(nom):
    coll = bpy.data.collections.get(nom)
    if coll is None:
        raise ValueError(f"Collection introuvable : {nom!r}")
    return [o for o in coll.all_objects if o.type == 'MESH']


def trouver_immeubles():
    if COLLECTION_IMMEUBLES:
        return _objets_collection(COLLECTION_IMMEUBLES)
    return [o for o in bpy.context.scene.objects
            if o.type == 'MESH' and _correspond(o.name, MOTS_CLES_IMMEUBLE)
            and not _correspond(o.name, MOTS_CLES_FENETRE)]


def trouver_fenetres(immeubles):
    if COLLECTION_FENETRES:
        candidats = _objets_collection(COLLECTION_FENETRES)
    elif UTILISER_SELECTION and bpy.context.selected_objects:
        candidats = [o for o in bpy.context.selected_objects if o.type == 'MESH']
    else:
        candidats = [o for o in bpy.context.scene.objects
                     if o.type == 'MESH' and _correspond(o.name, MOTS_CLES_FENETRE)]
    exclus = set(immeubles)
    return [o for o in candidats if o not in exclus]


def bvh_monde(obj, depsgraph):
    """Arbre BVH de l'objet (modificateurs appliqués) en coordonnées monde."""
    eval_obj = obj.evaluated_get(depsgraph)
    mesh = eval_obj.to_mesh()
    bm = bmesh.new()
    bm.from_mesh(mesh)
    bm.transform(obj.matrix_world)
    bm.normal_update()
    arbre = BVHTree.FromBMesh(bm)
    bm.free()
    eval_obj.to_mesh_clear()
    return arbre


def sommets_monde(obj, depsgraph):
    eval_obj = obj.evaluated_get(depsgraph)
    mesh = eval_obj.to_mesh()
    mw = obj.matrix_world
    pts = [mw @ v.co for v in mesh.vertices]
    eval_obj.to_mesh_clear()
    return pts


def centre(points):
    c = Vector()
    for p in points:
        c += p
    return c / len(points)


def axe_le_plus_fin(obj):
    """Axe local (unitaire, en coordonnées monde) selon lequel la fenêtre est la plus fine."""
    dims = obj.dimensions
    i = min(range(3), key=lambda k: dims[k])
    axe = Vector((0.0, 0.0, 0.0))
    axe[i] = 1.0
    return (obj.matrix_world.to_3x3() @ axe).normalized()


def tourner_autour(obj, rotation, pivot):
    """Applique une rotation (Quaternion, monde) à l'objet autour d'un pivot monde."""
    mw = obj.matrix_world.copy()
    loc, rot, scale = mw.decompose()
    nouvelle_rot = rotation @ rot
    nouvelle_loc = pivot + rotation @ (loc - pivot)
    obj.matrix_world = (Matrix.Translation(nouvelle_loc)
                        @ nouvelle_rot.to_matrix().to_4x4()
                        @ Matrix.Diagonal(scale).to_4x4())


def plaquer():
    depsgraph = bpy.context.evaluated_depsgraph_get()
    immeubles = trouver_immeubles()
    if not immeubles:
        raise RuntimeError("Aucun immeuble trouvé : vérifiez MOTS_CLES_IMMEUBLE ou COLLECTION_IMMEUBLES.")
    fenetres = trouver_fenetres(immeubles)
    if not fenetres:
        raise RuntimeError("Aucune fenêtre trouvée : vérifiez MOTS_CLES_FENETRE, la sélection ou COLLECTION_FENETRES.")

    print(f"{len(immeubles)} immeuble(s), {len(fenetres)} fenêtre(s) à traiter")
    arbres = [(b, bvh_monde(b, depsgraph)) for b in immeubles]

    traitees, ignorees = 0, []
    for fen in fenetres:
        pts = sommets_monde(fen, depsgraph)
        if not pts:
            ignorees.append(fen.name)
            continue
        c = centre(pts)

        # Point le plus proche sur l'ensemble des immeubles.
        meilleur = None
        for b, arbre in arbres:
            loc, normale, _idx, dist = arbre.find_nearest(c)
            if loc is not None and (meilleur is None or dist < meilleur[3]):
                meilleur = (b, loc, normale, dist)
        if meilleur is None or meilleur[3] > DISTANCE_MAX:
            ignorees.append(fen.name)
            continue
        _b, point_mur, normale, _dist = meilleur
        normale = normale.normalized()
        # Normale orientée vers l'extérieur, côté fenêtre (robuste aux normales inversées).
        if (c - point_mur).dot(normale) < 0:
            normale = -normale

        # 1) Rotation : rendre la fenêtre parallèle au mur.
        if ALIGNER_ROTATION:
            axe = axe_le_plus_fin(fen)
            if axe.dot(normale) < 0:
                axe = -axe
            rotation = axe.rotation_difference(normale)
            if rotation.angle > 1e-6:
                tourner_autour(fen, rotation, c)
                bpy.context.view_layer.update()
                pts = sommets_monde(fen, depsgraph)

        # 2) Translation : la face arrière de la fenêtre vient toucher le mur.
        arriere = min((p - point_mur).dot(normale) for p in pts)
        delta = normale * (DECALAGE - arriere)
        fen.matrix_world.translation += delta
        traitees += 1

    bpy.context.view_layer.update()
    print(f"Fenêtres plaquées : {traitees}")
    if ignorees:
        print(f"Ignorées ({len(ignorees)}) : {', '.join(ignorees)}")
    return traitees, ignorees


if __name__ == "__main__":
    plaquer()
