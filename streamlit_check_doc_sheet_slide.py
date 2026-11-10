import os
import re
import json
import time
import streamlit as st
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from google.oauth2 import service_account

# Configuration de la page Streamlit
st.set_page_config(
    page_title="Analyseur de liens Google Drive",
    page_icon="🔗",
    layout="wide"
)

# --- CONFIGURATION DE LA SÉCURITÉ / OAUTH WEB ---
# .streamlit/secrets.toml attendu :
#   [auth]
#   allowed_emails = ["vous@gmail.com"]
#   [google_oauth]
#   client_id = "...apps.googleusercontent.com"
#   client_secret = "..."
#   redirect_uri = "http://localhost:8501"   # ou l'URL de l'app déployée (sans slash final)
AUTHORIZED_EMAILS = [e.lower() for e in st.secrets.get("auth", {}).get("allowed_emails", [])]

SCOPES = [
    'openid',
    'https://www.googleapis.com/auth/userinfo.email',
    'https://www.googleapis.com/auth/documents.readonly',
    'https://www.googleapis.com/auth/spreadsheets.readonly',
    'https://www.googleapis.com/auth/presentations.readonly',
    'https://www.googleapis.com/auth/drive.readonly'
]

# Google peut renvoyer les scopes dans un ordre/ensemble différent : ne pas planter
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
if st.secrets.get("google_oauth", {}).get("redirect_uri", "").startswith("http://localhost"):
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")  # localhost uniquement

STATE_TTL = 600  # secondes


@st.cache_resource
def _pending_states():
    """États OAuth en attente (partagés entre sessions : la session Streamlit est
    perdue pendant la redirection vers Google, donc session_state ne convient pas)."""
    return {}


def build_flow():
    cfg = st.secrets["google_oauth"]
    client_config = {
        "web": {
            "client_id": cfg["client_id"],
            "client_secret": cfg["client_secret"],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [cfg["redirect_uri"]],
        }
    }
    # PKCE désactivé : le code_verifier serait perdu entre l'aller et le retour
    # (nouvelle session Streamlit). Le client_secret protège l'échange.
    return Flow.from_client_config(
        client_config,
        scopes=SCOPES,
        redirect_uri=cfg["redirect_uri"],
        autogenerate_code_verifier=False,
    )


def logout():
    for k in ("creds_json", "user_email"):
        st.session_state.pop(k, None)


def get_credentials():
    """Retourne des credentials valides, ou None (callback OAuth traité ici)."""
    # 1. Credentials déjà en session
    raw = st.session_state.get("creds_json")
    if raw:
        creds = Credentials.from_authorized_user_info(json.loads(raw), SCOPES)
        if not creds.valid and creds.refresh_token:
            try:
                creds.refresh(Request())
                st.session_state.creds_json = creds.to_json()
            except RefreshError:
                logout()
                return None
        if creds.valid:
            return creds
        logout()
        return None

    # 2. Retour de Google (?code=...&state=...)
    params = st.query_params
    if "code" in params:
        state = params.get("state")
        pending = _pending_states()
        created = pending.pop(state, None)
        code = params["code"]
        st.query_params.clear()
        if created is None or time.time() - created > STATE_TTL:
            st.error("Session OAuth invalide ou expirée, veuillez réessayer.")
            return None
        try:
            flow = build_flow()
            flow.fetch_token(code=code)
        except Exception as e:
            st.error(f"Échec de l'échange du code OAuth : {e}")
            return None
        st.session_state.creds_json = flow.credentials.to_json()
        st.rerun()
    return None


def show_login_button():
    flow = build_flow()
    auth_url, state = flow.authorization_url(
        access_type="offline",
        prompt="select_account consent",
        include_granted_scopes="false",
    )
    pending = _pending_states()
    now = time.time()
    for s in [s for s, t in pending.items() if now - t > STATE_TTL]:
        pending.pop(s, None)
    pending[state] = now
    st.sidebar.link_button("Se connecter avec Google", auth_url)


def check_access():
    """Authentifie via Google OAuth et vérifie l'e-mail vérifié contre la liste blanche."""
    st.session_state.setdefault("user_email", None)
    creds = get_credentials()

    if creds is None:
        st.sidebar.title("Connexion")
        show_login_button()
        return False

    if not st.session_state.user_email:
        info = build('oauth2', 'v2', credentials=creds).userinfo().get().execute()
        email = (info.get("email") or "").lower()
        if not info.get("verified_email") or email not in AUTHORIZED_EMAILS:
            logout()
            st.error(f"Accès non autorisé pour {email or 'cet utilisateur'}.")
            return False
        st.session_state.user_email = email

    st.sidebar.success(f"Connecté en tant que :\n**{st.session_state.user_email}**")
    if st.sidebar.button("Se déconnecter"):
        logout()
        st.rerun()
    return True


SERVICE_ACCOUNT_FILE = 'credentials_service_account.json'

def authenticate_google_apis():
    """Compte de service (non utilisé par défaut)."""
    if os.path.exists(SERVICE_ACCOUNT_FILE):
        return service_account.Credentials.from_service_account_file(
            SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    if "gcp_service_account" in st.secrets:
        creds_dict = dict(st.secrets["gcp_service_account"])
        return service_account.Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    raise FileNotFoundError("Aucun identifiant de compte de service valide n'a été trouvé.")


def extract_links_from_doc(docs_service, document_id):
    document = docs_service.documents().get(documentId=document_id).execute()
    content = document.get('body', {}).get('content', [])
    links_info = []
    
    def search_elements(elements):
        for elem in elements:
            if 'paragraph' in elem:
                for p_elem in elem['paragraph'].get('elements', []):
                    link = p_elem.get('textRun', {}).get('textStyle', {}).get('link', {})
                    if 'url' in link:
                        links_info.append({
                            'url': link['url'],
                            'location': 'Google Doc (Corps du texte)'
                        })
            elif 'table' in elem:
                for row in elem['table'].get('tableRows', []):
                    for cell in row.get('tableCells', []):
                        search_elements(cell.get('content', []))

    search_elements(content)
    return links_info

def col_to_letter(col_idx):
    let = ""
    col_idx += 1
    while col_idx > 0:
        col_idx, remainder = divmod(col_idx - 1, 26)
        let = chr(65 + remainder) + let
    return let

def extract_links_from_sheet(sheets_service, spreadsheet_id):
    spreadsheet = sheets_service.spreadsheets().get(
        spreadsheetId=spreadsheet_id, includeGridData=True
    ).execute()
    links_info = []
    
    for sheet in spreadsheet.get('sheets', []):
        sheet_title = sheet.get('properties', {}).get('title', 'Feuille')
        for grid_data in sheet.get('data', []):
            start_row = grid_data.get('startRow', 0)
            start_col = grid_data.get('startColumn', 0)
            
            for r_offset, row in enumerate(grid_data.get('rowData', [])):
                row_num = start_row + r_offset + 1
                for c_offset, cell in enumerate(row.get('values', [])):
                    col_num = start_col + c_offset
                    cell_coord = f"{sheet_title}!{col_to_letter(col_num)}{row_num}"
                    
                    if 'hyperlink' in cell:
                        links_info.append({
                            'url': cell['hyperlink'],
                            'location': cell_coord
                        })
                    
                    for run in cell.get('textFormatRuns', []):
                        link_uri = run.get('format', {}).get('link', {}).get('uri')
                        if link_uri:
                            links_info.append({
                                'url': link_uri,
                                'location': cell_coord
                            })
    return links_info

def extract_links_from_slide(slides_service, presentation_id):
    presentation = slides_service.presentations().get(presentationId=presentation_id).execute()
    links_info = []
    
    for index, slide in enumerate(presentation.get('slides', []), start=1):
        slide_name = f"Diapositive {index}"
        
        def parse_text_elements(text_elements):
            for text_elem in text_elements:
                link = text_elem.get('textRun', {}).get('style', {}).get('link', {})
                if 'url' in link:
                    links_info.append({
                        'url': link['url'],
                        'location': slide_name
                    })

        for element in slide.get('pageElements', []):
            if 'shape' in element:
                parse_text_elements(element['shape'].get('text', {}).get('textElements', []))
            elif 'table' in element:
                for row in element['table'].get('tableRows', []):
                    for cell in row.get('tableCells', []):
                        parse_text_elements(cell.get('text', {}).get('textElements', []))
                        
    return links_info

def extract_links(services, file_id, mime_type):
    if mime_type == 'application/vnd.google-apps.document':
        return extract_links_from_doc(services['docs'], file_id)
    elif mime_type == 'application/vnd.google-apps.spreadsheet':
        return extract_links_from_sheet(services['sheets'], file_id)
    elif mime_type == 'application/vnd.google-apps.presentation':
        return extract_links_from_slide(services['slides'], file_id)
    else:
        raise ValueError(f"Type de fichier non pris en charge : {mime_type}")

def extract_drive_file_id(url):
    patterns = [
        r'drive\.google\.com/(?:file/d/|folders/)([a-zA-Z0-9_-]+)',
        r'docs\.google\.com/(?:document|spreadsheets|presentation)/d/([a-zA-Z0-9_-]+)'
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None

def get_drive_path(drive_service, file_id):
    path_components = []
    current_id = file_id

    while current_id:
        file_meta = drive_service.files().get(
            fileId=current_id, 
            fields='name, parents',
            supportsAllDrives=True
        ).execute()
        
        path_components.append(file_meta.get('name'))
        parents = file_meta.get('parents', [])
        
        if parents:
            current_id = parents[0]
        else:
            break

    path_components.reverse()
    return "/" + "/".join(path_components)

def compute_lowest_common_ancestor(paths):
    if not paths:
        return None

    split_paths = [p.strip('/').split('/') for p in paths]
    min_len = min(len(p) for p in split_paths)
    lca_components = []

    for i in range(min_len):
        current_segment = split_paths[0][i]
        if all(path[i] == current_segment for path in split_paths):
            lca_components.append(current_segment)
        else:
            break

    if not lca_components:
        return "/"
    
    return "/" + "/".join(lca_components)


# --- INTERFACE STREAMLIT ---
def main_app():
    st.title("📂 Analyseur de liens Google Drive")
    st.write("Sélectionnez ou saisissez les identifiants de fichiers Google Drive à analyser.")

    # Dictionnaire de préréglages (basé sur votre code d'origine)
    presets = {
        "Comptabilité GF/OG": [
            '1QS2ElXD3AmKwG6Tvj1nNpBccxhiPbKV0Bs_9C1nFQ6Q',
            '1hYptwVF4my9--PFTWYakfg2O3yzo4CUEagK2JxMlquU'
        ],
        "Closeaux": [
            '1KdbkKCbM-ZCqjrYLR6njtZ6lu_qHTmr4C0Hdms1mbkE',
            '1hFuPctJMOK9GF03fRqptPvVLPNnUGQ8280lZgg2xdnE'
        ],
        "Aliénation": [
            '1I382krgfEi_utLQ13MULyYZbIVIBgWaE5TtV0uLn5vQ',
            '17y-A9FWmtQXcGpp6KUcTsPmxgI6qTYT2X5qDKpM7eZ0'
        ],
        "Messimy": [
            '11Fnc3aKqwgt9RQiZhL8xBFlxn2Vo3bhm9gNbLWiIzrI'
        ]
    }

    selected_preset = st.selectbox("Choisir un groupe prédéfini :", list(presets.keys()))
    file_ids_input = st.text_area(
        "Ou saisissez les IDs Google Drive (un par ligne) :",
        value="\n".join(presets[selected_preset])
    )

    if st.button("Lancer l'analyse"):
        file_ids = [fid.strip() for fid in file_ids_input.split("\n") if fid.strip()]
        
        if not file_ids:
            st.warning("Veuillez fournir au moins un ID de fichier.")
            return

        with st.spinner("Authentification et analyse en cours..."):
            try:
                creds = get_credentials()
                services = {
                    'docs': build('docs', 'v1', credentials=creds),
                    'sheets': build('sheets', 'v4', credentials=creds),
                    'slides': build('slides', 'v1', credentials=creds),
                    'drive': build('drive', 'v3', credentials=creds)
                }
            except Exception as e:
                st.error(f"Erreur d'authentification : {e}")
                return

            for FILE_ID in file_ids:
                try:
                    file_metadata = services['drive'].files().get(
                        fileId=FILE_ID, fields='name, mimeType', supportsAllDrives=True
                    ).execute()
                    mime_type = file_metadata.get('mimeType')
                    file_name = file_metadata.get('name')
                    
                    st.subheader(f"📄 Fichier : {file_name}")
                    st.text((f"Type : {mime_type} | ID : {FILE_ID}"))

                    links_info = extract_links(services, FILE_ID, mime_type)
                    st.info(f"Total d'hyperliens trouvés : {len(links_info)}")

                    accessible_paths = []
                    external_or_inaccessible = []

                    for item in links_info:
                        url = item['url']
                        location = item['location']
                        target_file_id = extract_drive_file_id(url)
                    
                        if target_file_id:
                            try:
                                path = get_drive_path(services['drive'], target_file_id)
                                accessible_paths.append({'location': location, 'url': url, 'path': path})
                            except Exception as e:
                                external_or_inaccessible.append({
                                    'location': location,
                                    'url': url,
                                    'reason': f"Inaccessible ou introuvable ({e})"
                                })
                        else:
                            external_or_inaccessible.append({
                                'location': location,
                                'url': url,
                                'reason': "Lien externe au Google Drive"
                            })

                    # Affichage des résultats sous forme de métriques et tableaux propres
                    col1, col2 = st.columns(2)
                    col1.metric("Liens internes résolus", len(accessible_paths))
                    col2.metric("Liens externes / inaccessibles", len(external_or_inaccessible))

                    if accessible_paths:
                        st.markdown("#### 🔗 Liens internes résolus")
                        st.dataframe(accessible_paths, use_container_width=True)
                        lca = compute_lowest_common_ancestor([p['path'] for p in accessible_paths])
                        st.success(f"**Lowest Common Ancestor (LCA) :** `{lca}`")

                    if external_or_inaccessible:
                        st.markdown("#### ⚠️ Liens externes ou inaccessibles")
                        st.dataframe(external_or_inaccessible, use_container_width=True)

                    st.divider()

                except Exception as e:
                    st.error(f"Erreur lors du traitement du fichier {FILE_ID} : {e}")

# Point d'entrée principal
if __name__ == '__main__':
    if check_access():
        main_app()
