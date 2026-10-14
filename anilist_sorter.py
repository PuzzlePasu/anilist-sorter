import streamlit as st
import requests
import json
import os
import time

st.set_page_config(page_title="AniList Duell-Sorter", layout="wide")

ANILIST_URL = "https://graphql.anilist.co"
DATA_DIR = "data"

# --- LOKALE KONFIGURATION (USERNAME & TOKEN) ---
def get_config_file():
    os.makedirs(DATA_DIR, exist_ok=True)
    return os.path.join(DATA_DIR, "config.json")

def load_config():
    config_file = get_config_file()
    if os.path.exists(config_file):
        with open(config_file, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"username": "", "token": ""}

def save_config(username, token):
    config_file = get_config_file()
    with open(config_file, "w", encoding="utf-8") as f:
        json.dump({"username": username.strip(), "token": token.strip()}, f, indent=2)

# --- API FUNKTIONEN ---
def fetch_completed_list(username, media_type):
    query = """
    query ($username: String,$type: MediaType) {
      MediaListCollection(userName: $username, type:$type, status: COMPLETED) {
        lists {
          entries {
            id
            mediaId
            score(format: POINT_100)
            media {
              title {
                romaji
                english
              }
              coverImage {
                extraLarge
              }
              format
              episodes
              chapters
            }
          }
        }
      }
    }
    """
    variables = {"username": username, "type": media_type}
    response = requests.post(ANILIST_URL, json={"query": query, "variables": variables})
    if response.status_code != 200:
        st.error(f"Fehler beim Abruf von AniList (Code {response.status_code}). Stimmt der Username '{username}'?")
        return []
    
    data = response.json()
    entries = []
    lists = data.get("data", {}).get("MediaListCollection", {}).get("lists", [])
    for lst in lists:
        for entry in lst.get("entries", []):
            title = entry["media"]["title"]["english"] or entry["media"]["title"]["romaji"]
            entries.append({
                "entry_id": entry["id"],
                "media_id": entry["mediaId"],
                "title": title,
                "romaji": entry["media"]["title"]["romaji"],
                "cover": entry["media"]["coverImage"]["extraLarge"],
                "format": entry["media"]["format"] or "UNKNOWN",
                "original_score": entry["score"] or 0
            })
    return entries

def update_anilist_score(token, media_id, score):
    mutation = """
    mutation ($mediaId: Int,$scoreRaw: Int) {
      SaveMediaListEntry(mediaId: $mediaId, scoreRaw:$scoreRaw) {
        id
        score
      }
    }
    """
    headers = {"Authorization": f"Bearer {token.strip()}"}
    variables = {"mediaId": media_id, "scoreRaw": int(score)}
    res = requests.post(ANILIST_URL, json={"query": mutation, "variables": variables}, headers=headers)
    return res.status_code == 200

# --- LOKALE DATENVERWALTUNG (RANKINGS) ---
def get_db_file(username, media_type):
    os.makedirs(DATA_DIR, exist_ok=True)
    return os.path.join(DATA_DIR, f"ranking_{username.lower()}_{media_type.lower()}.json")

def load_state(username, media_type):
    filename = get_db_file(username, media_type)
    if os.path.exists(filename):
        with open(filename, "r", encoding="utf-8") as f:
            data = json.load(f)
            if "anchors" not in data:
                data["anchors"] = {"90": None, "75": None, "50": None}
            if "ignored" not in data:
                data["ignored"] = []
            return data
    return {
        "ranked": [],
        "unranked": [],
        "ignored": [],
        "current": None,
        "low": 0,
        "high": -1,
        "anchors": {"90": None, "75": None, "50": None}
    }

def save_state(username, media_type, state):
    filename = get_db_file(username, media_type)
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)

def sync_with_anilist(username, media_type, state, auto_ignore_formats):
    fetched = fetch_completed_list(username, media_type)
    known_ids = (
        {x["media_id"] for x in state["ranked"]} |
        {x["media_id"] for x in state["unranked"]} |
        {x["media_id"] for x in state["ignored"]}
    )
    if state["current"]:
        known_ids.add(state["current"]["media_id"])
    
    new_items = [x for x in fetched if x["media_id"] not in known_ids]
    new_items.sort(key=lambda x: x["original_score"], reverse=True)
    
    added_unranked = 0
    added_ignored = 0
    for item in new_items:
        if item["format"] in auto_ignore_formats:
            state["ignored"].append(item)
            added_ignored += 1
        else:
            state["unranked"].append(item)
            added_unranked += 1
            
    save_state(username, media_type, state)
    return added_unranked, added_ignored

# --- ANKER-SCORE BERECHNUNG ---
def calculate_scores(ranked_list, anchors=None, min_score=35, max_score=100):
    n = len(ranked_list)
    if n == 0:
        return []
    if n == 1:
        ranked_list[0]["calculated_score"] = max_score
        return ranked_list

    points = [(0, max_score)]

    if anchors:
        id_to_idx = {item["media_id"]: idx for idx, item in enumerate(ranked_list)}
        anchor_candidates = []
        for score_str, media_id in anchors.items():
            if media_id is not None and media_id in id_to_idx:
                anchor_candidates.append((id_to_idx[media_id], int(score_str)))
        
        anchor_candidates.sort(key=lambda x: x[0])

        last_idx, last_score = 0, max_score
        for idx, sc in anchor_candidates:
            if idx > last_idx and sc < last_score and idx < n - 1 and sc > min_score:
                points.append((idx, sc))
                last_idx, last_score = idx, sc

    points.append((n - 1, min_score))

    for i in range(len(points) - 1):
        idx_start, score_start = points[i]
        idx_end, score_end = points[i + 1]

        for idx in range(idx_start, idx_end + 1):
            if idx_end == idx_start:
                score = score_start
            else:
                t = (idx - idx_start) / (idx_end - idx_start)
                score = score_start + t * (score_end - score_start)
            ranked_list[idx]["calculated_score"] = round(score)

    return ranked_list

# --- STREAMLIT UI & LOGIK ---
config = load_config()

st.sidebar.title("⚙️ Einstellungen")
username_input = st.sidebar.text_input("AniList Username", value=config.get("username", ""))

if username_input.strip() != config.get("username", ""):
    save_config(username_input, config.get("token", ""))
    config["username"] = username_input.strip()
    st.sidebar.toast("Username in data/config.json gespeichert!", icon="💾")

username = config["username"]
media_type = st.sidebar.radio("Kategorie", ["ANIME", "MANGA"])
min_score = st.sidebar.slider(
    "Mindest-Score für den letzten Platz", 1, 70, 35,
    help="Bestimmt die Punktzahl für den allerletzten Platz deiner Liste."
)

st.sidebar.divider()
st.sidebar.subheader("🚫 Format-Filter")
if media_type == "ANIME":
    format_options = ["OVA", "SPECIAL", "ONA", "MUSIC", "TV_SHORT", "MOVIE", "TV"]
    default_ignored_formats = ["OVA", "SPECIAL", "MUSIC"]
else:
    format_options = ["ONE_SHOT", "NOVEL", "MANGA"]
    default_ignored_formats = ["ONE_SHOT"]

auto_ignore_formats = st.sidebar.multiselect(
    "Beim Laden & per Schnellfilter ignorieren:",
    options=format_options,
    default=default_ignored_formats,
    help="Titel mit diesen Formaten werden beim Sync oder Klick auf den Schnellfilter direkt auf die Ignorier-Liste gesetzt."
)

if not username:
    st.info("👈 Bitte gib links deinen AniList-Benutzernamen ein (er wird danach automatisch lokal in `data/config.json` gespeichert).")
    st.stop()

state_key = f"state_{username}_{media_type}"
db_exists = os.path.exists(get_db_file(username, media_type))

if state_key not in st.session_state:
    st.session_state[state_key] = load_state(username, media_type)

state = st.session_state[state_key]

if not db_exists and not state["ranked"] and not state["unranked"] and not state["ignored"] and state["current"] is None:
    with st.spinner(f"Erster Start: Lade abgeschlossene {media_type} für {username} von AniList..."):
        added_u, added_i = sync_with_anilist(username, media_type, state, auto_ignore_formats)
        st.toast(f"{added_u} Titel geladen ({added_i} OVAs/Specials automatisch ignoriert)!", icon="✅")

if st.sidebar.button("🔄 Neue abgeschlossene Titel laden", use_container_width=True):
    with st.spinner(f"Lade {media_type} für {username} von AniList..."):
        added_unranked, added_ignored = sync_with_anilist(username, media_type, state, auto_ignore_formats)
        st.sidebar.success(f"{added_unranked} neue Titel geladen ({added_ignored} automatisch ignoriert)!")
        st.rerun()

if auto_ignore_formats and st.sidebar.button("🧹 Warteschlange nach Formaten bereinigen", use_container_width=True):
    moved_count = 0
    remaining_unranked = []
    for item in state["unranked"]:
        if item["format"] in auto_ignore_formats:
            state["ignored"].append(item)
            moved_count += 1
        else:
            remaining_unranked.append(item)
    state["unranked"] = remaining_unranked

    if state["current"] and state["current"]["format"] in auto_ignore_formats:
        state["ignored"].append(state["current"])
        state["current"] = None
        moved_count += 1

    save_state(username, media_type, state)
    st.sidebar.info(f"{moved_count} Titel aus der Warteschlange ignoriert.")
    st.rerun()

if state["current"] is None and len(state["unranked"]) > 0:
    if len(state["ranked"]) == 0:
        state["ranked"].append(state["unranked"].pop(0))
    
    if len(state["unranked"]) > 0:
        state["current"] = state["unranked"].pop(0)
        state["low"] = 0
        state["high"] = len(state["ranked"]) - 1
    save_state(username, media_type, state)

total_items = len(state["ranked"]) + len(state["unranked"]) + (1 if state["current"] else 0)
ranked_count = len(state["ranked"])
ignored_count = len(state["ignored"])

st.title(f"🏆 {username}s {media_type.capitalize()} Ranking ({ranked_count} / {total_items} einsortiert | {ignored_count} ignoriert)")
if total_items > 0:
    st.progress(ranked_count / total_items)

tab_duel, tab_list, tab_ignored, tab_sync = st.tabs([
    "⚔️ Duell",
    "📊 Rangliste & Anker-Scores",
    f"🚫 Ignorierte Titel ({ignored_count})",
    "☁️ AniList Upload"
])

# --- TAB 1: DUELL (BINÄRE SUCHE) ---
with tab_duel:
    if state["current"] is None:
        if total_items == 0:
            st.warning("Noch keine Titel geladen. Klicke links auf **'🔄 Neue abgeschlossene Titel laden'**.")
        else:
            st.success("🎉 Alle aktiven Titel sind vollständig einsortiert!")
    else:
        mid = (state["low"] + state["high"]) // 2
        candidate = state["current"]
        opponent = state["ranked"][mid]
        
        remaining_clicks = (state["high"] - state["low"] + 1).bit_length()
        st.caption(f"Noch maximal **{remaining_clicks} Entscheidungen** für diesen Titel (Suchbereich: Platz {state['low']+1} bis {state['high']+1})")
        
        col1, col_vs, col2 = st.columns([4, 1, 4])
        
        with col1:
            st.subheader("🆕 Einzuordnender Titel")
            st.image(candidate["cover"], width=260)
            st.markdown(f"### {candidate['title']}")
            st.caption(f"Romaji: {candidate['romaji']} | Format: **{candidate['format']}**")
            
            if st.button("👈 Dieser ist besser", use_container_width=True, type="primary"):
                state["high"] = mid - 1
                if state["low"] > state["high"]:
                    state["ranked"].insert(state["low"], candidate)
                    state["current"] = None
                save_state(username, media_type, state)
                st.rerun()

            if st.button(f"🚫 Titel ignorieren ({candidate['format']})", use_container_width=True):
                state["ignored"].append(candidate)
                state["current"] = None
                save_state(username, media_type, state)
                st.rerun()

        with col_vs:
            st.markdown("<h2 style='text-align: center; margin-top: 100px;'>VS</h2>", unsafe_allow_html=True)
            # NEU: Button für Gleichstand direkt in der Mitte
            if st.button("🤝 Beide gleich gut", use_container_width=True, help="Setzt den neuen Titel direkt auf denselben Platz wie den Gegner."):
                state["ranked"].insert(mid, candidate)
                state["current"] = None
                save_state(username, media_type, state)
                st.rerun()

        with col2:
            st.subheader(f"🛡️ Aktuell Platz #{mid + 1}")
            st.image(opponent["cover"], width=260)
            st.markdown(f"### {opponent['title']}")
            st.caption(f"Romaji: {opponent['romaji']} | Format: **{opponent['format']}**")
            
            if st.button("Dieser ist besser 👉", use_container_width=True, type="primary"):
                state["low"] = mid + 1
                if state["low"] > state["high"]:
                    state["ranked"].insert(state["low"], candidate)
                    state["current"] = None
                save_state(username, media_type, state)
                st.rerun()

            if st.button(f"🗑️ Gegner aus Rangliste ignorieren ({opponent['format']})", use_container_width=True):
                removed = state["ranked"].pop(mid)
                state["ignored"].append(removed)
                for k, v in state["anchors"].items():
                    if v == removed["media_id"]:
                        state["anchors"][k] = None
                state["low"] = 0
                state["high"] = len(state["ranked"]) - 1
                if len(state["ranked"]) == 0:
                    state["ranked"].append(candidate)
                    state["current"] = None
                save_state(username, media_type, state)
                st.rerun()

# --- TAB 2: RANGLISTE & ANKER ---
with tab_list:
    if len(state["ranked"]) == 0:
        st.info("Noch keine Einträge in der Rangliste.")
    else:
        st.subheader("⚓ Anker-Punkte definieren")
        st.caption(
            "Wähle aus deiner bereits sortierten Liste feste Referenz-Titel für **90**, **75** und **50** Punkte. "
            "Alle Titel dazwischen werden automatisch passend skaliert. (Platz 1 hat immer 100 Punkte)."
        )

        media_ids = [None] + [item["media_id"] for item in state["ranked"]]
        id_to_label = {None: "--- Kein Anker (Linear) ---"}
        for idx, item in enumerate(state["ranked"]):
            id_to_label[item["media_id"]] = f"#{idx + 1} - {item['title']}"

        col_a90, col_a75, col_a50 = st.columns(3)
        anchors_changed = False

        for col, score_key, label in [
            (col_a90, "90", "🌟 90-Punkte-Anker (Meisterwerk-Grenze)"),
            (col_a75, "75", "👍 75-Punkte-Anker (Gut / Solide)"),
            (col_a50, "50", "😐 50-Punkte-Anker (Mittelmaß)")
        ]:
            with col:
                current_val = state["anchors"].get(score_key)
                if current_val not in media_ids:
                    current_val = None
                selected_id = st.selectbox(
                    label,
                    options=media_ids,
                    index=media_ids.index(current_val),
                    format_func=lambda x: id_to_label.get(x, str(x)),
                    key=f"anchor_{username}_{media_type}_{score_key}"
                )
                if selected_id != state["anchors"].get(score_key):
                    state["anchors"][score_key] = selected_id
                    anchors_changed = True

        if anchors_changed:
            save_state(username, media_type, state)
            st.rerun()

        id_to_idx = {item["media_id"]: idx for idx, item in enumerate(state["ranked"])}
        idx_90 = id_to_idx.get(state["anchors"].get("90"), -1)
        idx_75 = id_to_idx.get(state["anchors"].get("75"), -1)
        idx_50 = id_to_idx.get(state["anchors"].get("50"), -1)

        if (idx_90 != -1 and idx_75 != -1 and idx_75 <= idx_90) or \
           (idx_75 != -1 and idx_50 != -1 and idx_50 <= idx_75) or \
           (idx_90 != -1 and idx_50 != -1 and idx_50 <= idx_90):
            st.warning(
                "⚠️ **Achtung:** Deine Anker-Reihenfolge widerspricht deiner Rangliste "
                "(z. B. ist dein 75er-Anker höher platziert als dein 90er-Anker). "
                "Widersprüchliche Anker werden bei der Berechnung ignoriert, bis du sie anpasst!"
            )

        st.divider()

        scored_list = calculate_scores(state["ranked"], anchors=state["anchors"], min_score=min_score, max_score=100)
        anchor_id_to_type = {v: f"⚓ {k}P" for k, v in state["anchors"].items() if v is not None}

        table_data = []
        for idx, item in enumerate(scored_list):
            marker = "🏆 100P (Top)" if idx == 0 else anchor_id_to_type.get(item["media_id"], "")
            if idx == len(scored_list) - 1 and not marker:
                marker = f"🔻 {min_score}P (Min)"

            table_data.append({
                "Rang": idx + 1,
                "Anker": marker,
                "Titel": item["title"],
                "Romaji": item["romaji"],
                "Format": item["format"],
                "Berechneter Score (1-100)": item["calculated_score"],
                "Alter AniList Score": item["original_score"]
            })
        st.dataframe(table_data, use_container_width=True, hide_index=True)

# --- TAB 3: IGNORIERTE TITEL ---
with tab_ignored:
    if len(state["ignored"]) == 0:
        st.info("Aktuell befinden sich keine Titel auf der Ignorier-Liste.")
    else:
        st.caption("Ignorierte Titel erhalten keinen neuen Score und werden beim AniList-Upload komplett übersprungen. Du kannst einzelne Titel hier jederzeit zurück in die Duell-Warteschlange holen.")
        
        restore_options = {item["media_id"]: f"{item['title']} ({item['format']})" for item in state["ignored"]}
        col_sel, col_btn = st.columns([3, 1])
        with col_sel:
            to_restore = st.multiselect(
                "Titel zum Wiederherstellen auswählen:",
                options=list(restore_options.keys()),
                format_func=lambda x: restore_options[x]
            )
        with col_btn:
            st.write("")
            st.write("")
            if st.button("♻️ Ausgewählte wiederherstellen", disabled=not to_restore, use_container_width=True):
                restored_items = [x for x in state["ignored"] if x["media_id"] in to_restore]
                state["ignored"] = [x for x in state["ignored"] if x["media_id"] not in to_restore]
                state["unranked"].extend(restored_items)
                save_state(username, media_type, state)
                st.rerun()

        ignored_table = [{
            "Titel": item["title"],
            "Romaji": item["romaji"],
            "Format": item["format"],
            "Bisheriger AniList Score": item["original_score"]
        } for item in state["ignored"]]
        st.dataframe(ignored_table, use_container_width=True, hide_index=True)

# --- TAB 4: ANILIST UPLOAD ---
with tab_sync:
    st.subheader(f"☁️ Scores für {username} synchronisieren")
    
    saved_token = config.get("token", "")
    col_tok, col_save = st.columns([4, 1])
    
    with col_tok:
        token = st.text_input(
            "AniList Access Token (aus data/config.json)",
            value=saved_token,
            type="password",
            help="Wird lokal in 'data/config.json' gespeichert und niemals zu GitHub hochgeladen."
        )
    with col_save:
        st.write("")
        st.write("")
        if st.button("💾 Token speichern", use_container_width=True, disabled=(token == saved_token)):
            save_config(username, token)
            config["token"] = token.strip()
            st.toast("Token lokal in data/config.json gespeichert!", icon="🔒")
            st.rerun()

    if saved_token:
        st.caption("✅ Token & Username sind lokal in `data/config.json` hinterlegt.")
    else:
        st.caption("ℹ️ Noch kein Token hinterlegt. Füge ihn oben ein und klicke auf **'💾 Token speichern'**.")
    
    scored_list = calculate_scores(state["ranked"], anchors=state["anchors"], min_score=min_score, max_score=100)
    to_update = [x for x in scored_list if x["calculated_score"] != x["original_score"]]
    
    st.info(f"📊 Aktuell sind **{len(scored_list)} Titel** in deiner Rangliste einsortiert. Davon haben **{len(to_update)} Titel** einen abweichenden neuen Score, der noch nicht auf AniList gespeichert ist.")

    if st.button("🚀 Abweichende Scores jetzt zu AniList hochladen", disabled=(not token or len(to_update) == 0), type="primary"):
        if token != saved_token:
            save_config(username, token)
            
        progress_bar = st.progress(0)
        status_text = st.empty()
        success_count = 0
        
        for i, item in enumerate(to_update):
            status_text.text(f"Aktualisiere ({i+1}/{len(to_update)}): {item['title']} ({item['original_score']} -> {item['calculated_score']}/100)")
            success = update_anilist_score(token, item["media_id"], item["calculated_score"])
            if success:
                item["original_score"] = item["calculated_score"]
                success_count += 1
            else:
                st.warning(f"Fehler beim Update von {item['title']} (Rate-Limit oder ungültiger Token).")
            time.sleep(0.8)
            progress_bar.progress((i + 1) / len(to_update))
            
        save_state(username, media_type, state)
        status_text.text("Upload abgeschlossen!")
        st.success(f"✅ {success_count} von {len(to_update)} Bewertungen erfolgreich auf AniList aktualisiert!")