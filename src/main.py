import json
import threading
import time
import traceback
from datetime import datetime

import flet as ft
import requests

try:
    import flet_geolocator as ftg   # extension GPS : à déclarer dans pyproject.toml
except ImportError:
    ftg = None

# ==========================================
# CONFIGURATION
# ==========================================
FIREBASE_URL = "https://k-haime-suivi-48bc4-default-rtdb.firebaseio.com"
TIMEOUT = 10                       # secondes avant abandon d'une requête
CLE_CAMION = "khaim_chauffeur_camion"
INTERVALLES = [("30", "30 secondes"), ("60", "1 minute"), ("120", "2 minutes"), ("300", "5 minutes")]
INTERVALLE_PAR_DEFAUT = 60
TAILLE_JOURNAL = 8


class Couleurs:
    DARK_BLUE = "#0B1B36"
    YELLOW = "#FFC107"
    WHITE = "#FFFFFF"
    GREEN = "#2E9D5B"
    RED = "#D64545"
    ORANGE = "#F28C28"
    TEXT_GRAY = "#6C757D"
    BG_GRAY = "#F4F6FA"
    ALERTE_BG = "#FDECEA"


# ==========================================
# FIREBASE
# ==========================================
def fb_get(chemin):
    """Retourne les données ({} si vide) ou None en cas d'erreur de connexion."""
    try:
        r = requests.get(f"{FIREBASE_URL}/{chemin}.json", timeout=TIMEOUT)
        if r.status_code == 200:
            return r.json() or {}
    except (requests.RequestException, ValueError):
        pass
    return None


def fb_put(chemin, donnees):
    try:
        return requests.put(f"{FIREBASE_URL}/{chemin}.json", json=donnees, timeout=TIMEOUT).status_code == 200
    except requests.RequestException:
        return False


# ==========================================
# OUTILS
# ==========================================
class GpsErreur(Exception):
    """Erreur de localisation avec un message lisible par le chauffeur."""


def texte(valeur, defaut=""):
    return str(valeur) if valeur not in (None, "") else defaut


# ==========================================
# APPLICATION
# ==========================================
def main_app(page: ft.Page):
    page.title = "K-HAIM Chauffeur"
    page.theme_mode = ft.ThemeMode.LIGHT
    page.bgcolor = Couleurs.DARK_BLUE
    page.padding = 0

    etat = {
        "camion_id": None, "camion_info": {},
        "suivi": False, "session": 0,
        "intervalle": INTERVALLE_PAR_DEFAUT,
        "dernier": None,           # (heure d'envoi, précision en m)
        "journal": [],             # (heure, message, ok)
    }
    ui = {}
    verrou_gps = threading.Lock()

    try:
        geo = ftg.Geolocator() if ftg else ft.Geolocator()
        page.overlay.append(geo)
    except Exception:
        geo = None

    def ic(nom):
        """Icône par son nom, avec une icône de secours si le nom n'existe pas."""
        return getattr(ft.Icons, nom, ft.Icons.CIRCLE)

    # ---------- composants communs ----------
    def notifier(message, couleur=Couleurs.GREEN):
        page.open(ft.SnackBar(ft.Text(message), bgcolor=couleur))
        page.update()

    def carte(contenu, **kw):
        return ft.Container(
            content=contenu,
            bgcolor=Couleurs.WHITE,
            border_radius=12,
            padding=14,
            shadow=ft.BoxShadow(blur_radius=6, color=ft.Colors.with_opacity(0.06, "black")),
            **kw,
        )

    def pastille(texte_, couleur):
        return ft.Container(
            content=ft.Text(texte_, size=11, color=Couleurs.WHITE, weight=ft.FontWeight.BOLD),
            bgcolor=couleur,
            padding=ft.padding.symmetric(horizontal=10, vertical=5),
            border_radius=6,
        )

    def bouton(texte_, icone, couleur, action, texte_sombre=False, **kw):
        return ft.ElevatedButton(
            texte_, icon=icone, on_click=action,
            bgcolor=couleur,
            color=Couleurs.DARK_BLUE if texte_sombre else Couleurs.WHITE,
            style=ft.ButtonStyle(
                shape=ft.RoundedRectangleBorder(radius=12),
                text_style=ft.TextStyle(weight=ft.FontWeight.BOLD),
            ),
            **kw,
        )

    def confirmer(titre, message, action, libelle="Confirmer", couleur=Couleurs.GREEN):
        def oui(e):
            page.close(dlg)
            action()

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text(titre),
            content=ft.Text(message),
            actions=[
                ft.TextButton("Annuler", on_click=lambda e: page.close(dlg)),
                ft.ElevatedButton(libelle, bgcolor=couleur, color=Couleurs.WHITE, on_click=oui),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        page.open(dlg)

    def icone_camion(info, taille=44):
        citerne = texte(info.get("categorie")) == "Citerne"
        return ft.Container(
            content=ft.Icon(ic("LOCAL_GAS_STATION") if citerne else ic("LOCAL_SHIPPING"), color=Couleurs.WHITE, size=22),
            bgcolor=Couleurs.ORANGE if citerne else Couleurs.DARK_BLUE,
            width=taille, height=taille, border_radius=taille / 2, alignment=ft.alignment.center,
        )

    # ---------- mémorisation du camion choisi ----------
    def lire_camion_enregistre():
        try:
            brut = page.client_storage.get(CLE_CAMION)
            if brut:
                donnees = json.loads(brut) if isinstance(brut, str) else brut
                return donnees["id"], donnees.get("info", {})
        except Exception:
            pass
        return None, {}

    def enregistrer_camion(camion_id, info):
        try:
            page.client_storage.set(CLE_CAMION, json.dumps({"id": camion_id, "info": info}))
        except Exception:
            pass

    def oublier_camion():
        try:
            page.client_storage.remove(CLE_CAMION)
        except Exception:
            pass

    # ==========================================
    # GPS
    # ==========================================
    def obtenir_position():
        """Retourne (latitude, longitude, précision en m, vitesse en km/h) ou lève GpsErreur."""
        if geo is None:
            raise GpsErreur("Le GPS n'est pas disponible dans cette version de l'application.")
        try:
            if not geo.is_location_service_enabled():
                raise GpsErreur("Le GPS du téléphone est désactivé. Activez la localisation.")
        except GpsErreur:
            raise
        except Exception:
            pass
        try:
            statut = geo.get_permission_status()
            nom = getattr(statut, "name", str(statut)).upper()
            if "DENIED" in nom:
                statut = geo.request_permission()
                nom = getattr(statut, "name", str(statut)).upper()
                if "DENIED" in nom:
                    raise GpsErreur("Autorisation de localisation refusée. Autorisez-la dans les réglages du téléphone.")
        except GpsErreur:
            raise
        except Exception:
            pass
        try:
            position = geo.get_current_position()
        except Exception as erreur:
            raise GpsErreur(f"GPS indisponible : {erreur}")
        if not position:
            raise GpsErreur("Position introuvable. Placez-vous à découvert et réessayez.")
        precision = getattr(position, "accuracy", None)
        vitesse = getattr(position, "speed", None)
        return (
            position.latitude, position.longitude,
            precision if isinstance(precision, (int, float)) else None,
            vitesse * 3.6 if isinstance(vitesse, (int, float)) and vitesse >= 0 else None,
        )

    def maj_ecran():
        """Met à jour l'écran de suivi s'il est affiché."""
        if not ui.get("badge"):
            return
        actif = etat["suivi"]
        ui["badge"].content.value = "SUIVI ACTIF" if actif else "SUIVI ARRÊTÉ"
        ui["badge"].bgcolor = Couleurs.GREEN if actif else Couleurs.TEXT_GRAY
        if etat["dernier"]:
            heure, precision = etat["dernier"]
            suffixe = f" • précision {round(precision)} m" if precision is not None else ""
            ui["dernier"].value = f"Dernier envoi : {heure}{suffixe}"
        else:
            ui["dernier"].value = "Aucun envoi pour le moment"
        ui["bouton_suivi"].text = "ARRÊTER LE SUIVI" if actif else "DÉMARRER LE SUIVI"
        ui["bouton_suivi"].bgcolor = Couleurs.RED if actif else Couleurs.GREEN
        ui["bouton_suivi"].icon = ic("STOP_CIRCLE") if actif else ic("PLAY_CIRCLE")
        ui["journal"].controls = [
            ft.Row([
                ft.Text(h, size=11, color=Couleurs.TEXT_GRAY, width=58),
                ft.Text(m, size=12, color=Couleurs.GREEN if ok else Couleurs.RED, expand=True),
            ], vertical_alignment=ft.CrossAxisAlignment.START)
            for h, m, ok in etat["journal"]
        ] or [ft.Text("Aucune activité pour le moment.", size=12, color=Couleurs.TEXT_GRAY)]
        page.update()

    def journaliser(message, ok=True):
        etat["journal"].insert(0, (datetime.now().strftime("%H:%M:%S"), message, ok))
        del etat["journal"][TAILLE_JOURNAL:]
        maj_ecran()

    def envoyer_position():
        """Envoie la position au patron. Retourne True si l'envoi a réussi."""
        if not etat["camion_id"]:
            return False
        if not verrou_gps.acquire(blocking=False):
            return False   # un envoi est déjà en cours
        try:
            try:
                lat, lng, precision, vitesse = obtenir_position()
            except GpsErreur as erreur:
                journaliser(str(erreur), False)
                return False
            info = etat["camion_info"]
            maintenant = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            donnees = {
                "camion": etat["camion_id"],
                "chauffeur": texte(info.get("chauffeur_nom"), "Inconnu"),
                "immatriculation": texte(info.get("immatriculation"), "N/A"),
                "categorie": texte(info.get("categorie"), "Marchandise"),
                "latitude": lat,
                "longitude": lng,
                "heure": maintenant,
            }
            if precision is not None:
                donnees["precision_m"] = round(precision)
            if vitesse is not None:
                donnees["vitesse_kmh"] = round(vitesse)
            if fb_put(f"positions/{etat['camion_id']}", donnees):
                etat["dernier"] = (maintenant[11:], precision)
                journaliser(f"Position envoyée ({lat:.4f}, {lng:.4f})", True)
                return True
            journaliser("Échec de l'envoi : vérifiez votre connexion internet.", False)
            return False
        finally:
            verrou_gps.release()

    def boucle_suivi(session):
        while etat["suivi"] and etat["session"] == session:
            envoyer_position()
            for _ in range(etat["intervalle"]):
                if not (etat["suivi"] and etat["session"] == session):
                    return
                time.sleep(1)

    def demarrer_suivi():
        if not etat["camion_id"] or etat["suivi"]:
            return
        etat["suivi"] = True
        etat["session"] += 1
        threading.Thread(target=boucle_suivi, args=(etat["session"],), daemon=True).start()
        journaliser("Suivi démarré")

    def arreter_suivi():
        if etat["suivi"]:
            etat["suivi"] = False
            etat["session"] += 1
            journaliser("Suivi arrêté")

    # ==========================================
    # ÉCRAN 1 : CHOIX DU CAMION
    # ==========================================
    def afficher_selection():
        arreter_suivi()
        ui.clear()
        page.clean()
        zone = ft.Column(spacing=10)
        donnees = {"camions": {}, "erreur": False, "recherche": ""}

        def choisir(camion_id, info):
            chauffeur = texte(info.get("chauffeur_nom"))
            message = f"Vous conduisez {camion_id} ({texte(info.get('immatriculation'), 'N/A')}) ?"
            if chauffeur:
                message += f"\nChauffeur assigné : {chauffeur}"

            def valider():
                etat["camion_id"] = camion_id
                etat["camion_info"] = info
                etat["dernier"] = None
                etat["journal"] = []
                enregistrer_camion(camion_id, info)
                afficher_suivi()

            confirmer("Confirmer le camion", message, valider, libelle="Oui, c'est mon camion")

        def carte_camion(camion_id, info):
            chauffeur = texte(info.get("chauffeur_nom"))
            return carte(ft.Column(
                [
                    ft.Row([
                        icone_camion(info),
                        ft.Column(
                            [
                                ft.Text(camion_id, weight=ft.FontWeight.BOLD, color=Couleurs.DARK_BLUE, size=16),
                                ft.Text(f"Immatriculation : {texte(info.get('immatriculation'), 'N/A')}", size=12, color=Couleurs.TEXT_GRAY),
                                ft.Text(f"{texte(info.get('categorie'), 'Marchandise')} • Capacité : {texte(info.get('capacite'), 'N/A')}", size=12, color=Couleurs.TEXT_GRAY),
                                ft.Text(
                                    f"Chauffeur : {chauffeur}" if chauffeur else "Chauffeur : non assigné",
                                    size=12, color=Couleurs.GREEN if chauffeur else Couleurs.RED, weight=ft.FontWeight.BOLD,
                                ),
                            ],
                            spacing=2, expand=True,
                        ),
                    ]),
                    ft.Row([
                        bouton("Choisir ce camion", ic("TOUCH_APP"), Couleurs.YELLOW,
                               lambda e, c=camion_id, i=info: choisir(c, i), texte_sombre=True, expand=True, height=46)
                    ]),
                ],
                spacing=10,
            ))

        def rendre():
            requete = donnees["recherche"].lower()
            elements = []
            if donnees["erreur"]:
                elements.append(ft.Container(
                    bgcolor=Couleurs.ALERTE_BG, border_radius=10, padding=12,
                    content=ft.Column([
                        ft.Text("Connexion impossible. Vérifiez votre internet puis réessayez.",
                                size=12, color=Couleurs.RED, weight=ft.FontWeight.BOLD),
                        ft.TextButton("Réessayer", icon=ic("REFRESH"), on_click=lambda e: charger()),
                    ], spacing=4),
                ))
            for cid, info in sorted(donnees["camions"].items()):
                if not isinstance(info, dict):
                    continue
                recherche = f"{cid} {info.get('immatriculation', '')} {info.get('chauffeur_nom', '')}".lower()
                if requete and requete not in recherche:
                    continue
                elements.append(carte_camion(cid, info))
            if not elements:
                elements.append(ft.Text(
                    "Aucun camion disponible. Contactez le patron." if not donnees["erreur"] else "",
                    color=Couleurs.TEXT_GRAY,
                ))
            zone.controls = elements
            page.update()

        def charger():
            zone.controls = [ft.Row([ft.ProgressRing(width=28, height=28)], alignment=ft.MainAxisAlignment.CENTER)]
            page.update()
            camions = fb_get("camions")
            if camions is None:
                donnees["erreur"] = True
            else:
                donnees.update(camions=camions, erreur=False)
            rendre()

        def sur_recherche(e):
            donnees["recherche"] = (e.control.value or "").strip()
            rendre()

        entete = ft.Container(
            bgcolor=Couleurs.DARK_BLUE,
            padding=ft.padding.only(left=20, right=4, top=8, bottom=14),
            content=ft.Row([
                ft.Column(
                    [
                        ft.Text("K-HAIM CHAUFFEUR", size=20, weight=ft.FontWeight.BOLD, color=Couleurs.WHITE),
                        ft.Text("Choisissez votre camion", size=12, color=Couleurs.YELLOW),
                    ],
                    spacing=2, expand=True,
                ),
                ft.IconButton(ic("REFRESH"), icon_color=Couleurs.YELLOW, tooltip="Actualiser", on_click=lambda e: charger()),
            ]),
        )
        page.add(
            ft.SafeArea(
                expand=True,
                content=ft.Column(
                    [
                        entete,
                        ft.Container(
                            bgcolor=Couleurs.BG_GRAY, expand=True,
                            padding=ft.padding.only(left=16, right=16, top=14),
                            content=ft.Column(
                                [
                                    ft.TextField(
                                        label="Rechercher (camion, plaque)", prefix_icon=ic("SEARCH"),
                                        border_radius=10, bgcolor=Couleurs.WHITE, on_change=sur_recherche,
                                    ),
                                    zone,
                                    ft.Container(height=20),
                                ],
                                spacing=12, scroll=ft.ScrollMode.AUTO, expand=True,
                            ),
                        ),
                    ],
                    spacing=0, expand=True,
                ),
            )
        )
        charger()

    # ==========================================
    # ÉCRAN 2 : SUIVI GPS
    # ==========================================
    def afficher_suivi():
        ui.clear()
        page.clean()
        info = etat["camion_info"]
        chauffeur = texte(info.get("chauffeur_nom"))

        ui["badge"] = pastille("SUIVI ARRÊTÉ", Couleurs.TEXT_GRAY)
        ui["dernier"] = ft.Text("Aucun envoi pour le moment", size=13, color=Couleurs.DARK_BLUE)
        ui["journal"] = ft.Column(spacing=6)

        def basculer(e):
            if etat["suivi"]:
                arreter_suivi()
            else:
                demarrer_suivi()

        def envoyer_maintenant(e):
            if etat["suivi"] is False and not verrou_gps.locked():
                journaliser("Recherche de votre position GPS…")
            ok = envoyer_position()
            if ok:
                notifier("Position envoyée au patron")

        def sur_intervalle(e):
            etat["intervalle"] = int(e.control.value)

        def changer_camion(e):
            def valider():
                oublier_camion()
                etat["camion_id"] = None
                etat["camion_info"] = {}
                afficher_selection()

            texte_confirmation = "Le suivi sera arrêté et vous devrez choisir un autre camion."
            confirmer("Changer de camion", texte_confirmation, valider, libelle="Changer", couleur=Couleurs.ORANGE)

        ui["bouton_suivi"] = bouton(
            "DÉMARRER LE SUIVI", ic("PLAY_CIRCLE"), Couleurs.GREEN, basculer, height=64, expand=True,
        )

        entete = ft.Container(
            bgcolor=Couleurs.DARK_BLUE,
            padding=ft.padding.only(left=20, right=4, top=8, bottom=14),
            content=ft.Row([
                ft.Column(
                    [
                        ft.Text("K-HAIM CHAUFFEUR", size=20, weight=ft.FontWeight.BOLD, color=Couleurs.WHITE),
                        ft.Text("Partage de position GPS", size=12, color=Couleurs.YELLOW),
                    ],
                    spacing=2, expand=True,
                ),
                ft.IconButton(ic("SWAP_HORIZ"), icon_color=Couleurs.YELLOW, tooltip="Changer de camion", on_click=changer_camion),
            ]),
        )

        carte_camion_actuel = carte(ft.Row([
            icone_camion(info, 48),
            ft.Column(
                [
                    ft.Text(etat["camion_id"], weight=ft.FontWeight.BOLD, color=Couleurs.DARK_BLUE, size=17),
                    ft.Text(f"{texte(info.get('immatriculation'), 'N/A')} • {texte(info.get('categorie'), 'Marchandise')}", size=12, color=Couleurs.TEXT_GRAY),
                    ft.Text(
                        f"Chauffeur : {chauffeur}" if chauffeur else "Chauffeur : non assigné",
                        size=12, color=Couleurs.GREEN if chauffeur else Couleurs.RED, weight=ft.FontWeight.BOLD,
                    ),
                ],
                spacing=2, expand=True,
            ),
        ]))

        carte_statut = carte(ft.Column(
            [
                ft.Row([ft.Text("État du suivi", size=13, color=Couleurs.TEXT_GRAY, expand=True), ui["badge"]]),
                ui["dernier"],
                ft.Dropdown(
                    label="Envoi automatique toutes les", value=str(etat["intervalle"]),
                    options=[ft.dropdown.Option(key=k, text=t) for k, t in INTERVALLES],
                    border_radius=10, bgcolor=Couleurs.WHITE, on_change=sur_intervalle,
                ),
                ft.Row([ui["bouton_suivi"]]),
                ft.Row([
                    bouton(
                        "Envoyer ma position maintenant", ic("MY_LOCATION"), Couleurs.YELLOW,
                        envoyer_maintenant, texte_sombre=True, expand=True, height=50,
                    )
                ]),
            ],
            spacing=12,
        ))

        note = ft.Container(
            bgcolor="#FFF6DB", border_radius=10, padding=12,
            content=ft.Text(
                "Pour que le patron vous voie en direct, gardez l'application ouverte "
                "et l'écran allumé, avec le GPS et internet activés.",
                size=12, color=Couleurs.DARK_BLUE,
            ),
        )

        carte_journal = carte(ft.Column(
            [ft.Text("Activité récente", size=13, weight=ft.FontWeight.BOLD, color=Couleurs.DARK_BLUE), ui["journal"]],
            spacing=8,
        ))

        page.add(
            ft.SafeArea(
                expand=True,
                content=ft.Column(
                    [
                        entete,
                        ft.Container(
                            bgcolor=Couleurs.BG_GRAY, expand=True,
                            padding=ft.padding.only(left=16, right=16, top=14),
                            content=ft.Column(
                                [carte_camion_actuel, carte_statut, note, carte_journal, ft.Container(height=20)],
                                spacing=12, scroll=ft.ScrollMode.AUTO, expand=True,
                            ),
                        ),
                    ],
                    spacing=0, expand=True,
                ),
            )
        )
        maj_ecran()

    # ==========================================
    # DÉMARRAGE
    # ==========================================
    def demarrer():
        camion_id, cache = lire_camion_enregistre()
        if not camion_id:
            afficher_selection()
            return
        frais = fb_get(f"camions/{camion_id}")
        if frais is None:               # hors ligne : on garde les infos mémorisées
            info = cache
        elif not frais:                 # camion supprimé par le patron
            oublier_camion()
            afficher_selection()
            return
        else:
            info = frais
            enregistrer_camion(camion_id, info)
        etat["camion_id"] = camion_id
        etat["camion_info"] = info
        afficher_suivi()

    page.update()
    demarrer()


def main(page: ft.Page):
    try:
        main_app(page)
    except Exception:
        page.add(ft.Text(traceback.format_exc(), selectable=True, color="red"))
        page.update()


ft.app(target=main)
