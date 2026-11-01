import os
import re
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from google.oauth2 import service_account
import streamlit as st

SCOPES = [
    'https://www.googleapis.com/auth/documents.readonly',
    'https://www.googleapis.com/auth/spreadsheets.readonly',
    'https://www.googleapis.com/auth/presentations.readonly',
    'https://www.googleapis.com/auth/drive.readonly'
]

#credentials.json dedicated to secret_client...
SERVICE_ACCOUNT_FILE = 'credentials_service_account-json'
SERVICE_ACCOUNT_EMAIL='succession@myfilter-513011.iam.gserviceaccount.com' #provided by SERVICE_ACCOUNT_FILE

def authenticate_google_apis():
    creds = None
    """Gère simplement l'authentification par service_account des API Google."""
    if False and os.path.exists(SERVICE_ACCOUNT_FILE):
        creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES)
        return creds
    """Gère l'authentification OAuth 2.0 pour les API Google (peut nécessiter un accord dans une fenêtre browser)."""
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                'credentials.json', SCOPES)
            creds = flow.run_local_server(port=0)
        with open('token.json', 'w') as token:
            token.write(creds.to_json())
    return creds

def extract_links_from_doc(docs_service, document_id):
    """Extrait les hyperliens d'un Google Doc avec leur localisation."""
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
    """Convertit un indice de colonne (0-indexé) en lettre(s) A1 (ex: 0 -> A, 26 -> AA)."""
    let = ""
    col_idx += 1
    while col_idx > 0:
        col_idx, remainder = divmod(col_idx - 1, 26)
        let = chr(65 + remainder) + let
    return let

def extract_links_from_sheet(sheets_service, spreadsheet_id):
    """Extrait les hyperliens d'un Google Sheet en identifiant précisément la cellule (notation A1)."""
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
                    
                    # Lien direct sur toute la cellule
                    if 'hyperlink' in cell:
                        links_info.append({
                            'url': cell['hyperlink'],
                            'location': cell_coord
                        })
                    
                    # Liens intégrés dans du texte riche (rich text runs)
                    for run in cell.get('textFormatRuns', []):
                        link_uri = run.get('format', {}).get('link', {}).get('uri')
                        if link_uri:
                            links_info.append({
                                'url': link_uri,
                                'location': cell_coord
                            })
                            
    return links_info

def extract_links_from_slide(slides_service, presentation_id):
    """Extrait les hyperliens d'une présentation Google Slides avec leur diapositive d'origine."""
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
    """Aiguille l'extraction vers le service adapté."""
    if mime_type == 'application/vnd.google-apps.document':
        return extract_links_from_doc(services['docs'], file_id)
    elif mime_type == 'application/vnd.google-apps.spreadsheet':
        return extract_links_from_sheet(services['sheets'], file_id)
    elif mime_type == 'application/vnd.google-apps.presentation':
        return extract_links_from_slide(services['slides'], file_id)
    else:
        raise ValueError(f"Type de fichier non pris en charge : {mime_type}")

def extract_drive_file_id(url):
    """Extrait l'ID d'un fichier ou dossier Google Drive à partir de son URL."""
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
    """Remonte l'arborescence depuis la racine du Drive pour un fichier/dossier donné."""
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
    """Calcule le plus bas ancêtre commun (LCA) à partir d'une liste de chemins absolus."""
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

def main(FILE_IDs:list):
 
    print("Authentification auprès des services Google...")
    creds = authenticate_google_apis()
    
    services = {
        'docs': build('docs', 'v1', credentials=creds),
        'sheets': build('sheets', 'v4', credentials=creds),
        'slides': build('slides', 'v1', credentials=creds),
        'drive': build('drive', 'v3', credentials=creds)
    }

    for FILE_ID in FILE_IDs:
        print(f"Vérification du fichier ID: {FILE_ID}...")
        file_metadata = services['drive'].files().get(fileId=FILE_ID, fields='name, mimeType', supportsAllDrives=True).execute()
        mime_type = file_metadata.get('mimeType')
        print(f"Fichier analysé : '{file_metadata.get('name' )}' (Type : {mime_type})\n")

        links_info = extract_links(services, FILE_ID, mime_type)
        print(f"Total d'hyperliens trouvés : {len(links_info)}\n")

        accessible_paths = []
        external_or_inaccessible = []

        for item in links_info:
            url = item['url']
            location = item['location']
            target_file_id = extract_drive_file_id(url)
        
            if target_file_id:
                try:
                    path = get_drive_path(services['drive'], target_file_id)
                    accessible_paths.append(path)
                    print(f"[Interne] [{location}] {url} -> {path}")
                except Exception as e:
                    print(f"[Inaccessible] [{location}] {url} -> Erreur : {e}")
                    external_or_inaccessible.append({
                        'location': location,
                        'url': url,
                        'reason': f"Inaccessible ou introuvable ({e})"
                    })
            else:
                print(f"[Externe] [{location}] {url}")
                external_or_inaccessible.append({
                    'location': location,
                    'url': url,
                    'reason': "Lien externe au Google Drive"
                })

        print("\n" + "="*40)
        print("--- RAPPORT DES RÉSULTATS ---")
        print("="*40)
        print(f"Nombre de liens internes résolus : {len(accessible_paths)}")
        print(f"Nombre de liens externes ou inaccessibles : {len(external_or_inaccessible)}")

        if external_or_inaccessible:
            print("\nDétail des cellules/éléments avec liens non accessibles ou externes :")
            for entry in external_or_inaccessible:
                print(f" - Emplacement : {entry['location']}")
                print(f"   URL         : {entry['url']}")
                print(f"   Motif       : {entry['reason']}")
                print("-" * 30)

        if accessible_paths:
            lca = compute_lowest_common_ancestor(accessible_paths)
            print(f"\nLowest Common Ancestor (LCA) des chemins accessibles : {lca}")
        else:
            print("\nAucun chemin Google Drive valide n'a pu être résolu pour calculer un LCA.")
        print("==========================================")

# Titre de l'application
st.title("Mon Scrutateur Google Drive")

st.markdown("Cette application permet de scruter vos fichiers Google Drive en ligne.")

# 1. Zone de configuration / Authentification
st.sidebar.header("Configuration")
# Si vous utilisez un fichier de credentials ou des clés d'API :
uploaded_file = st.sidebar.file_uploader("Télécharger le fichier de compte de service (JSON)", type=["json"])

# 2. Bouton d'action pour lancer la scrutation
if st.button("Lancer la scrutation"):
    if uploaded_file is not None:
        with st.spinner("Scrutation du Google Drive en cours..."):
            # --- VOTRE CODE DE SCRUTATION ACTUEL ---
            # Exemple fictif de récupération de résultats :
            resultats = ["Fichier_1.pdf", "Rapport_2026.docx"] 
            # ---------------------------------------
            
        st.success("Scrutation terminée avec succès !")
        
        # Affichage des résultats sous forme de tableau ou de liste
        st.subheader("Résultats trouvés :")
        for fichier in resultats:
            st.write(f"- {fichier}")
    else:
        st.error("Veuillez fournir un fichier d'identification valide.")



        
if __name__ == '__main__':
    closeaux = [
        '1KdbkKCbM-ZCqjrYLR6njtZ6lu_qHTmr4C0Hdms1mbkE',
        '1hFuPctJMOK9GF03fRqptPvVLPNnUGQ8280lZgg2xdnE',
        ]
    aliénation= [
        '1I382krgfEi_utLQ13MULyYZbIVIBgWaE5TtV0uLn5vQ',
        '17y-A9FWmtQXcGpp6KUcTsPmxgI6qTYT2X5qDKpM7eZ0',
        ]
    messimy= [
        '11Fnc3aKqwgt9RQiZhL8xBFlxn2Vo3bhm9gNbLWiIzrI',
        ]
    compta_GF_OG= [
        '1QS2ElXD3AmKwG6Tvj1nNpBccxhiPbKV0Bs_9C1nFQ6Q',
        '1hYptwVF4my9--PFTWYakfg2O3yzo4CUEagK2JxMlquU',
        ]
    main(compta_GF_OG)
