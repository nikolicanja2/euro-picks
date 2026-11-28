import streamlit as st
import pandas as pd
import plotly.express as px
import requests
from io import BytesIO
import html
import re
import unicodedata
import extra_streamlit_components as stx



# --- EURO PICKS: analiza uticaja odsutnih saigraca (ugradjeno u app.py) ---
def _ep_absence_chunks(values, size=100):
    values = list(dict.fromkeys(v for v in values if v))
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _ep_absence_fetch(sb_client, table, select, column, values, limit=10000):
    found = []
    for part in _ep_absence_chunks(values):
        if part:
            response = sb_client.table(table).select(select).in_(column, part).limit(limit).execute()
            found.extend(response.data or [])
    return found


def _ep_absence_number(value):
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def show_absence_impact(sb_client, selected_player_name):
    """Poredi statistiku izabranog igrača kroz sve istorijske nastupe saigrača."""
    st.subheader("Poeni sa saigračem i bez njega")
    try:
        players = (sb_client.table("players").select("id,full_name")
                   .ilike("full_name", str(selected_player_name)).limit(30).execute().data or [])
        target_ids = [p["id"] for p in players if p.get("id")]
        if not target_ids:
            st.info("Nema povezanih utakmica za izabranog igrača.")
            return

        target_rows = _ep_absence_fetch(
            sb_client, "player_boxscores",
            "game_id,player_id,team_id,minutes,pts,reb,ast,tpm,pir,dnp_reason,source",
            "player_id", target_ids
        )
        # SportAPI is the canonical season-wide source for both appearances and DNP.
        target_rows = [r for r in target_rows if r.get("game_id") and str(r.get("source") or "").lower() == "sportapi"]
        game_ids = list(dict.fromkeys(r["game_id"] for r in target_rows))
        if not game_ids:
            st.info("Nema sačuvanih utakmica za izabranog igrača.")
            return

        games = _ep_absence_fetch(
            sb_client, "games", "id,start_time,home_team_id,away_team_id", "id", game_ids
        )
        game_map = {g["id"]: g for g in games}
        source_rank = {"sportapi": 0, "sofascore": 1}
        target_by_game = {}
        for row in target_rows:
            if row.get("dnp_reason") or row.get("game_id") not in game_map:
                continue
            gid = row["game_id"]
            old = target_by_game.get(gid)
            if old is None or source_rank.get(str(row.get("source") or "").lower(), 9) < source_rank.get(str(old.get("source") or "").lower(), 9):
                target_by_game[gid] = row

        if not target_by_game:
            st.info("Nema odigranih utakmica izabranog igrača.")
            return

        all_rows = _ep_absence_fetch(
            sb_client, "player_boxscores",
            "game_id,player_id,team_id,minutes,pts,reb,ast,tpm,pir,dnp_reason,source",
            "game_id", list(target_by_game), limit=10000
        )
        all_rows = [r for r in all_rows if str(r.get("source") or "").lower() == "sportapi"]
        target_team_by_game = {gid: row.get("team_id") for gid, row in target_by_game.items()}
        teammate_ids = set()
        for row in all_rows:
            gid = row.get("game_id")
            if (gid in target_team_by_game and row.get("player_id") not in target_ids
                    and row.get("team_id") == target_team_by_game[gid] and row.get("player_id")):
                teammate_ids.add(row["player_id"])

        if not teammate_ids:
            st.info("Nema evidentiranih saigrača u istorijskim boxscore podacima.")
            return

        name_rows = _ep_absence_fetch(sb_client, "players", "id,full_name", "id", list(teammate_ids))
        names = {p["id"]: p.get("full_name") or p["id"] for p in name_rows}
        name_to_ids = {}
        for pid in teammate_ids:
            name_to_ids.setdefault(names.get(pid, pid), set()).add(pid)
        choices = sorted(name_to_ids)
        chosen = st.selectbox("Izaberi saigrača za poređenje", choices, key="ep_absence_teammate_history")
        chosen_ids = name_to_ids[chosen]

        stat_choice = st.selectbox(
            "Statistika za poređenje",
            ["Poeni", "Skokovi", "Asistencije", "Trojke", "PIR", "PRA"],
            key="ep_absence_stat_history"
        )
        stat_cols = {
            "Poeni": ("pts",), "Skokovi": ("reb",), "Asistencije": ("ast",),
            "Trojke": ("tpm",), "PIR": ("pir",), "PRA": ("pts", "reb", "ast")
        }

        states = {}
        for row in all_rows:
            if row.get("player_id") not in chosen_ids:
                continue
            gid = row.get("game_id")
            state = states.setdefault(gid, set())
            if row.get("dnp_reason"):
                state.add("dnp")
            elif (_ep_absence_number(row.get("minutes")) or 0) > 0:
                state.add("played")

        without_values, with_values = [], []
        for gid, target in target_by_game.items():
            status_set = states.get(gid, set())
            # Conflicting or missing evidence is deliberately excluded.
            if status_set not in ({"dnp"}, {"played"}):
                continue
            values = [_ep_absence_number(target.get(col)) for col in stat_cols[stat_choice]]
            if any(value is None for value in values):
                continue
            value = sum(values)
            if status_set == {"dnp"}:
                without_values.append(value)
            else:
                with_values.append(value)

        avg_with = sum(with_values) / len(with_values) if with_values else None
        avg_without = sum(without_values) / len(without_values) if without_values else None
        c1, c2 = st.columns(2)
        c1.metric("SA SAIGRAČEM", f"{avg_with:.1f}" if avg_with is not None else "—",
                  f"{len(with_values)} utakmica")
        c2.metric("BEZ SAIGRAČA", f"{avg_without:.1f}" if avg_without is not None else "—",
                  f"{len(without_values)} utakmica")
        if avg_with is None or avg_without is None:
            st.info("Nema dovoljno potvrđenih istorijskih utakmica za obe situacije.")
    except Exception as exc:
        st.warning(f"Analiza trenutno nije dostupna: {exc}")


st.set_page_config(page_title="EURO PICKS", page_icon="🏀", layout="wide")

# --- EURO PICKS access control (Supabase Auth + database-backed Premium) ---
from supabase import create_client
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo


def format_serbian_date(value, include_time=False):
    """Format dates consistently for the EURO PICKS interface."""
    if value is None or pd.isna(value):
        return "—"
    try:
        dt = pd.to_datetime(value, errors="coerce")
        if pd.isna(dt):
            return "—"
        if getattr(dt, "tzinfo", None) is not None:
            dt = dt.tz_convert("Europe/Belgrade")
        return dt.strftime("%d. %m. %Y. %H:%M" if include_time else "%d. %m. %Y.")
    except (TypeError, ValueError, OverflowError):
        return "—"


def get_supabase():
    try:
        url = st.secrets["SUPABASE_URL"]
        key = st.secrets["SUPABASE_PUBLISHABLE_KEY"]
        return create_client(url, key)
    except Exception:
        st.error("Nedostaju Supabase podešavanja. Proveri Streamlit Settings → Secrets.")
        st.stop()


supabase = get_supabase()
ADMIN_EMAIL = "anjanikolicfakultet@gmail.com"

# Trajna prijava: refresh token se cuva u browser cookie-ju, a access token
# ostaje samo u Streamlit sesiji. Supabase automatski rotira refresh token.
_auth_cookies = stx.CookieManager(key="europicks_auth_cookie_manager")
_REFRESH_COOKIE = "europicks_refresh_token"
_COOKIE_DAYS = 30


def save_login_session(auth_session, auth_user, remember=True):
    st.session_state["sb_access_token"] = auth_session.access_token
    st.session_state["sb_refresh_token"] = auth_session.refresh_token
    st.session_state["sb_user"] = {"id": auth_user.id, "email": auth_user.email}
    if remember:
        _auth_cookies.set(
            _REFRESH_COOKIE,
            auth_session.refresh_token,
            expires_at=datetime.now() + timedelta(days=_COOKIE_DAYS),
            key="europicks_save_login_cookie",
            same_site="lax",
            secure=True,
        )
    else:
        try:
            _auth_cookies.delete(_REFRESH_COOKIE, key="europicks_clear_old_login_cookie")
        except Exception:
            pass


def clear_login_session():
    for key in ["sb_access_token", "sb_refresh_token", "sb_user"]:
        st.session_state.pop(key, None)
    try:
        _auth_cookies.delete(_REFRESH_COOKIE, key="europicks_delete_login_cookie")
    except Exception:
        pass


def send_europicks_notification(subject, html_body):
    """Send an admin notification through Resend; app operation remains successful if email fails."""
    try:
        resend_key = st.secrets.get("RESEND_API_KEY", "")
        if not resend_key:
            return False, "RESEND_API_KEY nije podešen u Streamlit Secrets."
        response = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {resend_key}", "Content-Type": "application/json"},
            json={
                "from": "EURO PICKS <onboarding@resend.dev>",
                "to": [ADMIN_EMAIL],
                "subject": subject,
                "html": html_body,
            },
            timeout=15,
        )
        if response.ok:
            return True, None
        return False, f"Resend HTTP {response.status_code}: {response.text[:500]}"
    except Exception as exc:
        return False, str(exc)


def restore_login_from_cookie():
    """Restore a login after the browser/Streamlit session has been restarted."""
    if st.session_state.get("sb_access_token"):
        return
    try:
        refresh = _auth_cookies.get(_REFRESH_COOKIE)
    except Exception:
        refresh = None
    if not refresh:
        return
    try:
        client = get_supabase()
        refreshed = client.auth.refresh_session(refresh)
        if refreshed and refreshed.session and refreshed.user:
            save_login_session(refreshed.session, refreshed.user, remember=True)
            st.rerun()
    except Exception:
        clear_login_session()


def current_user_client():
    client = get_supabase()
    access = st.session_state.get("sb_access_token")
    refresh = st.session_state.get("sb_refresh_token")
    if access and refresh:
        try:
            response = client.auth.set_session(access, refresh)
            # Keep the rotated refresh token in the cookie when Supabase returns it.
            session = getattr(response, "session", None)
            auth_user = getattr(response, "user", None) or st.session_state.get("sb_user")
            if session and auth_user:
                save_login_session(session, auth_user, remember=True)
        except Exception:
            clear_login_session()
            st.rerun()
    return client


def auth_screen():
    st.title("🏀 EURO PICKS")
    st.subheader("Dobro došli")
    st.write("LIGHT pristup omogućava pregled početne stranice i Premium paketa. Analitika je dostupna Premium članovima.")
    login_tab, register_tab = st.tabs(["Prijava", "Napravi nalog"])
    with login_tab:
        email = st.text_input("Email", key="login_email")
        password = st.text_input("Lozinka", type="password", key="login_password")
        remember_login = st.checkbox("Ostani prijavljen/a na ovom uređaju", value=True, key="remember_login")
        if st.button("Prijavi se", type="primary", use_container_width=True):
            try:
                result = supabase.auth.sign_in_with_password({"email": email.strip(), "password": password})
                save_login_session(result.session, result.user, remember=remember_login)
                st.rerun()
            except Exception as e:
                st.error(f"Prijava nije uspela: {e}")
    with register_tab:
        new_email = st.text_input("Email adresa", key="register_email")
        new_password = st.text_input("Lozinka (najmanje 8 karaktera)", type="password", key="register_password")
        confirm = st.text_input("Ponovi lozinku", type="password", key="register_confirm")
        if st.button("Registruj se", use_container_width=True):
            if not new_email.strip() or len(new_password) < 8:
                st.warning("Unesi ispravnu email adresu i lozinku od najmanje 8 karaktera.")
            elif new_password != confirm:
                st.warning("Lozinke se ne podudaraju.")
            else:
                try:
                    result = supabase.auth.sign_up({"email": new_email.strip(), "password": new_password})
                    if result.session:
                        save_login_session(result.session, result.user, remember=True)
                        st.rerun()
                    else:
                        st.success("Nalog je napravljen. Proveri email i potvrdi adresu, pa se prijavi.")
                except Exception as e:
                    st.error(f"Registracija nije uspela: {e}")


restore_login_from_cookie()

if not st.session_state.get("sb_access_token"):
    auth_screen()
    st.stop()

sb = current_user_client()
user = st.session_state.get("sb_user") or {}
user_email = (user.get("email") or "").lower()
user_id = user.get("id")
try:
    profile_result = sb.table("profiles").select("role,premium_until,is_blocked,premium_expiry_logout_at").eq("id", user_id).single().execute()
    profile = profile_result.data or {}
except Exception:
    profile = {}

if profile.get("is_blocked"):
    st.error("Ovaj nalog je blokiran. Kontaktiraj EURO PICKS administraciju.")
    if st.button("Odjavi se"):
        clear_login_session()
        st.rerun()
    st.stop()

is_admin = profile.get("role") == "admin" and user_email == ADMIN_EMAIL
premium_until = profile.get("premium_until")
try:
    is_premium = bool(premium_until and datetime.fromisoformat(premium_until.replace("Z", "+00:00")) > datetime.now(timezone.utc))
except Exception:
    is_premium = False

# Provera istorije probnog perioda preko bezbedne Supabase RPC funkcije.
trial_used = False
trial_expires_at = None
trial_expires_dt = None
trial_status_available = False
if not is_admin:
    try:
        trial_status_result = sb.rpc("get_trial_status").execute()
        trial_status = trial_status_result.data or {}
        if isinstance(trial_status, list) and trial_status:
            trial_status = trial_status[0]
        if isinstance(trial_status, dict):
            trial_used = bool(trial_status.get("trial_used"))
            trial_expires_at = trial_status.get("trial_expires_at")
            trial_status_available = True
            if trial_expires_at:
                trial_expires_dt = datetime.fromisoformat(str(trial_expires_at).replace("Z", "+00:00"))
                if trial_expires_dt.tzinfo is None:
                    trial_expires_dt = trial_expires_dt.replace(tzinfo=timezone.utc)
    except Exception:
        trial_status_available = False

premium_until_dt = None
if premium_until:
    try:
        premium_until_dt = datetime.fromisoformat(str(premium_until).replace("Z", "+00:00"))
        if premium_until_dt.tzinfo is None:
            premium_until_dt = premium_until_dt.replace(tzinfo=timezone.utc)
    except Exception:
        premium_until_dt = None

# Kada Premium (placeni paket ili probnih 24h) istekne, korisnik se odjavljuje samo jednom.
# Marker u profilu sprecava ponovnu odjavu pri sledecem logovanju, kako bi mogao da zatrazi novi paket.
now_utc = datetime.now(timezone.utc)
expiry_logout_at = profile.get("premium_expiry_logout_at")
try:
    expiry_logout_dt = datetime.fromisoformat(str(expiry_logout_at).replace("Z", "+00:00")) if expiry_logout_at else None
    if expiry_logout_dt and expiry_logout_dt.tzinfo is None:
        expiry_logout_dt = expiry_logout_dt.replace(tzinfo=timezone.utc)
except Exception:
    expiry_logout_dt = None

premium_just_expired = bool(
    not is_admin and premium_until_dt and premium_until_dt <= now_utc
    and (expiry_logout_dt is None or expiry_logout_dt < premium_until_dt)
)
if premium_just_expired:
    try:
        sb.table("profiles").update({"premium_expiry_logout_at": now_utc.isoformat()}).eq("id", user_id).execute()
    except Exception as marker_error:
        st.error("Nije moguće evidentirati istek Premium pristupa. Proveri SQL migraciju u paketu.")
        st.stop()
    try:
        sb.auth.sign_out()
    except Exception:
        pass
    clear_login_session()
    st.warning("Premium pristup je istekao. Prijavi se ponovo da bi zatražio/la novi paket i pristup uz uplatu.")
    auth_screen()
    st.stop()

with st.sidebar:
    st.caption(f"Prijavljen: {user_email}")
    st.caption("ADMIN" if is_admin else ("PREMIUM" if is_premium else "LIGHT"))
    if st.button("Odjavi se", key="logout_button"):
        try:
            sb.auth.sign_out()
        except Exception:
            pass
        clear_login_session()
        st.rerun()

# Proverava istek tokom otvorene sesije (ukljucujuci pakete 30/90/365 dana i probnih 24h).
if (not is_admin and is_premium and premium_until_dt and hasattr(st, "fragment")):
    _ep_premium_until_watch = premium_until_dt
    _ep_user_id_watch = user_id

    @st.fragment(run_every="30s")
    def _ep_premium_expiry_watch():
        if datetime.now(timezone.utc) >= _ep_premium_until_watch:
            try:
                sb.table("profiles").update({"premium_expiry_logout_at": datetime.now(timezone.utc).isoformat()}).eq("id", _ep_user_id_watch).execute()
            except Exception:
                pass
            try:
                sb.auth.sign_out()
            except Exception:
                pass
            clear_login_session()
            st.rerun()

    _ep_premium_expiry_watch()

if not is_admin and not is_premium:
    st.title("🏀 EURO PICKS")
    st.subheader("LIGHT članstvo")
    st.write("Dobro došao/la u EURO PICKS. Analitika igrača, utakmica i protivnika dostupna je Premium članovima.")

    if not trial_status_available:
        st.warning("Provera probnog perioda nije dostupna. Pokreni SQL podešavanje za 24h probni period u Supabase SQL Editoru.")

    if trial_status_available and not trial_used:
        st.markdown("### Isprobaj EURO PICKS Premium")
        st.info("Dobijaš pun pristup svim Premium analizama i funkcijama tokom 24 sata. Probni period možeš iskoristiti samo jednom po potvrđenoj email adresi.")
        if st.button("Isprobaj besplatno 24 sata", type="primary", use_container_width=True):
            try:
                trial_result = sb.rpc("start_trial_24h").execute()
                trial_data = trial_result.data or {}
                if isinstance(trial_data, list) and trial_data:
                    trial_data = trial_data[0]
                st.success("Probni Premium pristup je aktiviran na 24 sata.")
                st.rerun()
            except Exception as trial_error:
                error_text = str(trial_error)
                if "TRIAL_ALREADY_USED" in error_text:
                    st.error("Ova email adresa je već iskoristila besplatni probni period.")
                elif "EMAIL_NOT_CONFIRMED" in error_text:
                    st.error("Prvo potvrdi email adresu, pa ponovo pokušaj.")
                else:
                    st.error(f"Probni period nije mogao da se aktivira: {trial_error}")
    elif trial_status_available and trial_used:
        st.info("Ova email adresa je već iskoristila besplatni probni period. Možeš nastaviti sa izborom Premium paketa.")

    st.markdown("### Izaberi Premium paket")
    plans = {
        "Premium – 1 mesec": 30,
        "Premium – 3 meseca": 90,
        "Premium – 12 meseci": 365,
    }
    chosen_plan = st.radio("Trajanje članstva", list(plans), horizontal=True)
    st.info("Nakon slanja zahteva dobićeš instrukcije za uplatu. Premium se aktivira tek kada administracija proveri uplatu.")
    payment_email = st.text_input("Email za kontakt", value=user_email)
    payment_note = st.text_input("Napomena (opciono)", placeholder="Npr. ime uplatioca")
    if st.button("Zatraži Premium pristup", type="primary", use_container_width=True):
        if not payment_email.strip():
            st.warning("Unesi email adresu.")
        else:
            try:
                sb.table("premium_requests").insert({
                    "user_id": user_id,
                    "email": payment_email.strip(),
                    "plan": chosen_plan,
                    "duration_days": plans[chosen_plan],
                    "note": payment_note.strip(),
                    "status": "pending",
                }).execute()
                email_html = (
                    "<h2>Novi EURO PICKS Premium zahtev</h2>"
                    f"<p><b>Korisnik:</b> {html.escape(payment_email.strip())}</p>"
                    f"<p><b>Paket:</b> {html.escape(chosen_plan)}</p>"
                    f"<p><b>Trajanje:</b> {plans[chosen_plan]} dana</p>"
                    f"<p><b>Napomena:</b> {html.escape(payment_note.strip() or '—')}</p>"
                    f"<p><b>Vreme:</b> {datetime.now(ZoneInfo('Europe/Belgrade')).strftime('%d.%m.%Y. %H:%M')}</p>"
                )
                sent, mail_error = send_europicks_notification("Novi Premium zahtev – EURO PICKS", email_html)
                st.success("Zahtev je poslat. Sačekaj instrukcije za uplatu i potvrdu administracije.")
                if sent:
                    st.info("Obaveštenje je poslato administratoru na email.")
                else:
                    st.warning("Zahtev je sačuvan, ali email obaveštenje nije poslato. Proveri Resend podešavanja. " + str(mail_error))
            except Exception as e:
                st.error(f"Zahtev nije sačuvan. Proveri da li je SQL podešavanje završeno: {e}")
    st.markdown("---")
    st.caption("Premium paketi: 30, 90 ili 365 dana. Pristup se aktivira ručno nakon potvrde uplate.")
    st.stop()

if is_admin:
    with st.expander("⚙️ ADMIN PANEL – Premium zahtevi", expanded=True):
        st.subheader("Zahtevi za Premium")
        try:
            reqs = sb.table("premium_requests").select("id,user_id,email,plan,duration_days,note,status,created_at").order("created_at", desc=True).execute().data or []
        except Exception as e:
            reqs = []
            st.error(f"Ne mogu da učitam zahteve: {e}")
        if not reqs:
            st.info("Trenutno nema Premium zahteva.")
        for req in reqs:
            with st.container(border=True):
                st.write(f"**{req.get('email')}** · {req.get('plan')} · Status: **{req.get('status')}**")
                if req.get("note"):
                    st.caption("Napomena: " + req["note"])
                c1, c2 = st.columns(2)
                if req.get("status") == "pending":
                    if c1.button("Odobri uplatu", key=f"approve_{req['id']}", type="primary"):
                        try:
                            now = datetime.now(timezone.utc)
                            old = sb.table("profiles").select("premium_until").eq("id", req["user_id"]).single().execute().data or {}
                            old_until = old.get("premium_until")
                            try:
                                old_dt = datetime.fromisoformat(old_until.replace("Z", "+00:00")) if old_until else now
                            except Exception:
                                old_dt = now
                            start = max(now, old_dt)
                            until = start + timedelta(days=int(req["duration_days"]))
                            sb.table("profiles").update({"premium_until": until.isoformat(), "premium_expiry_logout_at": None, "is_blocked": False}).eq("id", req["user_id"]).execute()
                            sb.table("premium_requests").update({"status": "approved", "reviewed_at": now.isoformat()}).eq("id", req["id"]).execute()
                            st.success(f"Premium aktiviran do {until.strftime('%d.%m.%Y')}.")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Odobravanje nije uspelo: {e}")
                    if c2.button("Odbij", key=f"reject_{req['id']}"):
                        try:
                            sb.table("premium_requests").update({"status": "rejected", "reviewed_at": datetime.now(timezone.utc).isoformat()}).eq("id", req["id"]).execute()
                            st.rerun()
                        except Exception as e:
                            st.error(f"Odbijanje nije uspelo: {e}")
        st.markdown("#### Pregled Premium korisnika")
        try:
            premium_rows = sb.table("profiles").select("email,premium_until,role,is_blocked").not_.is_("premium_until", "null").order("premium_until", desc=False).execute().data or []
            now_utc = datetime.now(timezone.utc)
            parsed_rows = []
            for row in premium_rows:
                if row.get("role") == "admin" or not row.get("premium_until"):
                    continue
                try:
                    expiry = datetime.fromisoformat(str(row["premium_until"]).replace("Z", "+00:00"))
                    if expiry.tzinfo is None:
                        expiry = expiry.replace(tzinfo=timezone.utc)
                    days_left = (expiry - now_utc).total_seconds() / 86400
                except (TypeError, ValueError):
                    continue
                if row.get("is_blocked"):
                    status = "Blokiran"
                elif days_left < 0:
                    status = "Istekao"
                elif days_left <= 7:
                    status = "Ističe uskoro"
                else:
                    status = "Aktivan"
                parsed_rows.append({
                    "Email": row.get("email") or "—",
                    "Datum isteka": format_serbian_date(row.get("premium_until")),
                    "Preostalo dana": max(0, int(days_left + 0.999)) if days_left >= 0 else 0,
                    "Status": status,
                    "_sort": expiry,
                })
            parsed_rows.sort(key=lambda item: item["_sort"])
            active_count = sum(x["Status"] == "Aktivan" for x in parsed_rows)
            soon_count = sum(x["Status"] == "Ističe uskoro" for x in parsed_rows)
            expired_count = sum(x["Status"] == "Istekao" for x in parsed_rows)
            m1, m2, m3 = st.columns(3)
            m1.metric("Aktivni", active_count)
            m2.metric("Ističu za 7 dana", soon_count)
            m3.metric("Istekli", expired_count)
            status_filter = st.selectbox("Prikaži korisnike", ["Svi", "Aktivni", "Ističe uskoro", "Istekli", "Blokirani"], key="premium_expiry_filter")
            status_map = {"Svi": None, "Aktivni": "Aktivan", "Ističe uskoro": "Ističe uskoro", "Istekli": "Istekao", "Blokirani": "Blokiran"}
            visible_rows = [x for x in parsed_rows if status_map[status_filter] is None or x["Status"] == status_map[status_filter]]
            if visible_rows:
                st.dataframe(pd.DataFrame([{k: v for k, v in x.items() if k != "_sort"} for x in visible_rows]), use_container_width=True, hide_index=True)
            else:
                st.info("Nema korisnika u izabranoj kategoriji.")
        except Exception as e:
            st.error(f"Ne mogu da učitam pregled Premium korisnika: {e}")

        st.markdown("#### Upravljanje korisnikom")
        target_email = st.text_input("Email korisnika za produženje ili blokadu", key="admin_target_email")
        extra_days = st.selectbox("Produži Premium za", [30, 90, 365], format_func=lambda x: {30:"30 dana",90:"90 dana",365:"365 dana"}[x])
        ac1, ac2 = st.columns(2)
        if ac1.button("Produži Premium") and target_email.strip():
            try:
                found = sb.table("profiles").select("id,premium_until").eq("email", target_email.strip().lower()).single().execute().data
                now = datetime.now(timezone.utc)
                try:
                    old_dt = datetime.fromisoformat(found["premium_until"].replace("Z", "+00:00")) if found.get("premium_until") else now
                except Exception:
                    old_dt = now
                until = max(now, old_dt) + timedelta(days=extra_days)
                sb.table("profiles").update({"premium_until": until.isoformat(), "premium_expiry_logout_at": None}).eq("id", found["id"]).execute()
                st.success(f"Premium produžen do {until.strftime('%d.%m.%Y')}.")
                st.rerun()
            except Exception as e:
                st.error(f"Korisnik nije pronađen ili izmena nije uspela: {e}")
        if ac2.button("Blokiraj nalog") and target_email.strip():
            try:
                found = sb.table("profiles").select("id").eq("email", target_email.strip().lower()).single().execute().data
                sb.table("profiles").update({"is_blocked": True, "premium_until": None}).eq("id", found["id"]).execute()
                st.success("Nalog je blokiran.")
                st.rerun()
            except Exception as e:
                st.error(f"Korisnik nije pronađen ili izmena nije uspela: {e}")

if not is_admin and not is_premium:
    st.stop()


st.markdown("""
<style>
 .stApp {
  background-color:#050505 !important;
  background-image:linear-gradient(rgba(0,0,0,.70),rgba(0,0,0,.82)),url("data:image/jpeg;base64,/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAQDAwQDAwQEBAQFBQQFBwsHBwYGBw4KCggLEA4RERAOEA8SFBoWEhMYEw8QFh8XGBsbHR0dERYgIh8cIhocHRz/2wBDAQUFBQcGBw0HBw0cEhASHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBz/wgARCATmBOYDASIAAhEBAxEB/8QAHAABAAEFAQEAAAAAAAAAAAAAAAECAwUGBwQI/8QAGwEBAAIDAQEAAAAAAAAAAAAAAAECAwQFBgf/2gAMAwEAAhADEAAAAfn8AAAAAAAAAAAABMAAAAAkgEwAAAAAAAAAAkhN2JsstlsOzqkdFzGpuckr7b7tbZ4d6e4XdfPxb09jnDl5H6OqseTl13pc1vzWnpiJ5hR1JMcn83YV6cU8ndacuLgPl+g/Pnw/P8dyxWzr8hdIw21q6gzOM2tOymL4QAAAAAAAAAAAABJAJgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABJAAAAAAABUUsps2vuaN6esbHzelx/YemV83f1DOZKnR3bdy14qXycaniNnB0SnlWN2dbslriHl2cHcbHD7ebB2rz8dZcfXaeSLV61VyRMdeu8bmtu1enhlWO/fL/wA/XsOXv9XCPdhzdqnkuX1djodOr5bV2/d5PXcx31XXenRta3EsF9EeTo6Hz1HZNU6XO0Zksdv82BagAAAAAAAAAAAAAAAAAAAABMAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAB6tvwbWkZjqmxcfr8223YnI6nnvRjNbPlI0PWOhpdbwnIfN0+f0XA6vT0NDJeCiNvTqiFsUoAEwAAkQCYTEiJISlAqmiSq7YROWzenzr7XTth4hVodD6Ev8AA9g5u/16NM2Tmb9/X9muVtyXUPojzdXm/OUdf0XtcjWldG7zgkAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAATAK4mirZ+haHS5jvW+XeF2sf7K/Bzd72xo+n9DT6jqWgU9jlZfEUx0+ZFNVOTBESmKUwAhEpQmIAExJMCQIBMSQkQmAkRIAJhJMTAgV+nyTF9s3DkdXP6P0F6/n/auN1+q0YDPcnpYTQOv07Wv85WPoDQO/xOevT5upyQmoEoAAEwAAAAAAAAAAAAAAAAAAAAAAAAAEwAAAEwAAAAAAAEwkivZOmc7p846LslzzvetXbesamfasJzTA9rmbhq1qvscu3F+rNr+WPeifBHvpmPBT7LV8fmi/RfHai5TNaYqSpiqEQkQCJAABEiJAAAAJAJEAQSASiSaqBdzmvzi2Osbb895Ti9rvMaBunB7FjnXV5vT5us9/5n6PhaWuW+nyQmAAAAAAAAAAAAAAAAAAAAAAAAAACYAAAAAAAAAAAAE+rouruaV0va/V5r0Nm7Rq2ht7RqWgYbvcjM4eHa49VU35ii/wCvddfZ0/ZevWtHZ0vPa3ndbZv4fJ147c91D6G8nT0fnyx1Hne5q4y37bGfV89N6i9LcVxMUpIiJEJCJTCJgJEJiEiQACJAAAAAqKZyNdq463m6zBVZXFppRFZu5HFzTL07d/nvLcTud2jTtu892te5b3enb1vmuOscz9P5zxja0wAAAAAAAAAAAAAAAAACRAAAAAAAAAAAAAAAAABkK28O37lu3A7+Kylfg4HY9mB0nTe1zNj1ul6DhCvJiXatow5sFvFzSMObq+7858vI6uz6LsfOsW30u9pPiX3LUre4ZIwm3+b1aWfabeF919Di+G+mPnr0fFwVv2+fc0vPReotS3FdMxCYmIJRAIkAAACREhCRBJABJFfoylq4PK+q/atFNa1KJkWKPTYifB46mPLEVQQImvYtanDs9uz/AM87v5z0XT8Vf9/G6PGNP+ltI7/G489vi7/BCagAAAAAAAAATEiAAAAAAAAAAAAAAAAAAAAAAS9XUdTd1bqvvr8p6SYxPLzc+a+GPUefQbugqpriar0ZbFnvbXlsDp7WLuab5t3RvbYyvO7+Mwm16lMbbkdNpwbW73NFUv0L0c59mLL0X2aXsvP2Ni83n2bY5Xzn4Nv1f0vC8Fv02djVtU3KJrTFUWrSkiEwAAAExIkQAmAmAXC/Xd9dqeqqqMmOiZiFKRTTcpI8GRpideZnE0yW0UxNShE1IFVVtE9M3bVty5fZuzzPoHmO95uR9zt5cXzTHTeb+s8zaGxqgAAAAAAAAJgAAAAAAAAAAAAAAAAAAAACop2DO9X4vaxecq8Xme76NH1nU+/yvT5Ud/ghagmJm5Tfpkv9t479Ic7d5Pyf24ze1J2DXc3NvZGR9HP7OJ8ucwOXBsl/H06nQzF3HX8O176bF/Hl82J2v1MeodF8mu11N+4p2Pjva4ng8/r8+5p2abmWviw2TzlNo0uL9hAIiSUJAAAkiJAAAys1nLXaMuGKaqZRTUhTFy2lTKERNIwmcw1beaJVyW0xWQAOtbtpO/4c/wA9bFgthmd42HhG7ea9H0HUdo9HI3/nHw/QXGvV+cwQ6fLAAAAAAAAAAAAAAAAAAAAAAAAAAAAHui3n6lmNs8z6Si5To3I38zybx+f1vnoiaejzJglUITVFVbXPZ58jg2ui39w+dtDaxMI7PHbPq+f1t73vLjNXfveLZMZm1/XRV7MO3hc1Xh5rsvr1fPanQyXsw/q1N/Yvfgsxz9n1cg7rxHt+X8Pn9Xl6/Liuc/lweOjLxenhtZGq9cVj9kxtseqDHkEkJIhIARIARIjbMNsOXFbpqi9KYmImKLlmGG8lNeLN6s35798cUzEw83roNfj0+bHlimqItAhCYOr9K5n1fFm+cNm1vacuPR6qLlsezdO4ZlOJ3u2+PHZ3yvf4jqP0xyj0nD58mO5wwAAAAAAABJAAAAAAAAAAAAAAAAABsGPLY7L68n5L1Cz5+S4r5PSVHrvNTSp2NUJhKRKa2m5Rdrf09N5t23Q3uaaDXb39BTMZMTJY25TLkPTjsnrb1+wnFno9uK90x6KvNXjz2Xp8l8Wd9up7Dq7+d2DVNo5XYynHegc66fnLNi5b7HIyuY0/15cWyTbrvSGN8NqZ7BY7xqwlW0SSiZEAAEkJhD32c1avsqoqy4qYqpTEShR4bOLpe3mbeViZt10XxwREzCSnCZTD1vTFVNbxEomAdO7PxTuOLJ8x7Tq+25aaJNUXxQSezpvKauZ1u/UaXuvjPTcs5v8AUHOO5y+SK6PReeCQAAAAAAAAAAAAAAAAAAAAAAA2vHmp7NV6/HepYmzyHLW9iKqPWeYpiYy4oiqERISVRKpNbVXKb1Mt/vHLNy5u7wyJp7HJhczuPNr0+rzWx3vdjPTj2PbV55w7PmyOL9d8Xrr89WLZv1+aut12uxE7dt2o5zid/Vtei36HycWarezqxSptSuimLVUzEwEwACAAISB7Ty5TJ+vJjhMZMdNNykoproiafBkcDS2PGPLsHoxOXy4qImlFNNVMTVC0Yqxc8+PLeporiaVUJpkOkd24J9BUt8sbrpW95ac+i6tjsrlsoprpiauj82r0ej9A1a/nvE+p59yX6d591+fx9ct+m84AAAAAAAAAAAAAAAAAAAAAANox5bvaLfs8b6trjk2zhjx12/V+apiYvipTBCqCJBUmsq6a4vd9VnIa+11/lffvlrVv4x1eblM9iPdodW9qWx37V1KcvjNnSqueWsv0TZifdVYrpmv3LXuxbFeesbXzO5reFnFdHg1UU07ugpim1FExaoiYRKYhMBJESgF8sJgTEkbNrW2Xp7qfP6c2G3FymJppmCiKoTGu7D5aW1qPVGLLfzWs+i1c3Rb8lq+7yYmilvd78Feifdi/TSnzXLiJoiumURNMTvf0d82fTVbfKO/6J0LLi5rXFdsdqz6bMTZpu0LW4qVfSerbXzzT6O2+jm/Q/Eep5/yX6f5p2OdyhVT6XzoAABMAAAAAAAAAAAAAAAAAzFL+zttrJeO9W127ybNjt+a5b9b5m3au0WxURIpVE0q6SmZlMVRVWZuU36ZL3Q9D+gNHdxnzf1Lls40HS5+wXrPq0Or5bVVnLrvdjqLR7MRksvTJrNW0X8ebSsrnJi/n2nD67q7W76J4G/yq5ttvTqiIVmIWiIlMIkiBIEC4W1+4jyzkcZIIkBmsJlZjYpqbGvFFWOrPthRKYtVxLy+jyVmNd9vjx5ITFb1eqzn7V8se2i1PDYyHgiabV63E24rpiaIrpWpiaYbj9SfLP1PFvlLonO+k5sXLb9v0TjsWPTZTYt+i3WbC7EW+k9F3rRjkm/c89Oh0+/NG3fw/qOT85+neL+i5GkpjvcIAAAAAAAAAAAAAAAAAAeiJ9PcfFtPkvUsT6OT4r+bxXbfsvLWrd23fFZmqgW7tJbqmIKYlKVZFSqtqvTY9OLPnuzabPM6OhaXs+sdDmhta2bz+ubbzetrHn9Hn2tW3TNGTCpmLUETE0zCBExUpSmaRKExMImJgkCExKAK/Z4NstXLYDZ7GbBzynddKw55FbIkRsGAiY3y3qlGTH68Rcqx5NlqxWby4qEpijWdp1PHezV7vJjy03bHsmL3myeFmNgnzem1I8Hv8ZZorpi1FFdMTRTVTE0iJ2j6k+Wfpw+Zeo8y6hlx8p9VHpyYvHY9nnrbz03qKz5qfRTE/ROl7jp63HKqYpb1dO5X7+V2e7+XF7B4z0Hz7rv0Vwb13Axo6vKAAAAAAAAAAAAAAAAAr61hes+c9FVZu874/QxGJ9FHuPKWLPq8ubX81NdELlFSa2abttKkhbrruptV1W4lNM1tdv+f04s/Z9A3/AF3mb+g63l8P1OdIza+wbhjMryO1pnm9Xn3tGxRcoy4aIqptRTVEwiYmsJplMJmImExMJEAEwCExJExdKdw1PMXptVm7Yz6+m+DJY3W2iJiUJBA9Pl2GYt0bBr18eN2rT87E5OmrzXrXrGXxOPJ6PIVtRm8NsNq+jy+lfHYX8rE4C10b1YNrlEdSuYNnmOQ2fC1yU14ixE5y3hkTtfdfl25MbN0Pjvqy4XvxHoz63qsVsuGzZ9XltjsUlbd/1TZdWOQ0TGO9d7zzjzZzrnCOlcPvdC1HZPR5jqfMVjrPJ/ceYgbWoAAAAAAAAAAAAAAA2jD945XW99+cT5P0GG0/O+b3XlcVTlvFuaOL8OQ8sPJF+mFiLtExbouUluK4hN2zMSogmqaZra7f897Hl7J5aeaaOzrcUz0+eiqU9bw+6814XovJ5/Z5+lzPJbv2s2vaprpvjpiqJrSmJqiUxCUoiqJiJmCEpiEwJETOU3G9MHl71GbDh8ZfweHNvXo03cs2HE6b0DTMeTxDFlAhIja9V91q7Rq2wYW9Mgx+NrbcdOqRNmqm7S1tkvDKM1s/UcWbnG2ZfXeN3tg1ny4HBvWLNvE7eplI1ruexqcsjK6Lkw7FZwUXx5WnFUzTZsV0PKHJbuc2a+PnNq/ZraIoRGQ92CrtGZseavNr9x1mrGZMeg3Mzh9PoeZ4vXn07nXuRdjrktbTpF3y/p914p27wc3J80stifceVC1AAAABJAAAAAAAAFyjfcGztm6258P6y1i9m2f1fC5n5Oja13eJpGE2LDKYexlPNS+Po91MMfZ9vnR56L1ETbXaUWqbltNNNVMTJFZuXbFyt9zu6Z2Hm9PglWQ8HS5a7amY3zf+DZLn9PaMR2bluPJgrPrsbul56LtGTDbitalua0xQvdOTyt072RHJI3HUrxaVxNaYqi0QkQXUend8dkc+CqKvFeukLOxa+fA9Cv282GcPlvFaNFVU6uyTAT7jHX8v7LUyev5y3lxaf79gppkwfh2q0jDZL0XS71zIYjh9u5hNf0Pm9jpWMs7N2OPxWd50fo8uaKqJpkNn8Vut9v5H0vVjX6aqZhs2s9WidP3rlO/p17f9W2nNh5KtV47ZLbcdhceXH+bL4iuRRdopki/4/TEqa6It5b9i/n1L/XeO7/krntF3znGvt9mzXHer+J9Tq3EPqLjvT5/O0x6bzwAAAAAAAAAAAAuQyneNd3HyPq4u+LqVq5G5VT63y3j1fcNGzYtDwm+4zPj0nxbji62121k/JWcJ5sl5qPFT7aU+W37fNDy2r1ooiqImlKJVU1Vt6N/5/sOpuZjl30r846mxYmHW5dUJNl7T849R5XW1fydN5tmxeS36rWzrWZv9phpm9eLimHP3fQ+Xxkxbd5tc9N6dr5vt281vwC3s2v7Gt5ovUXpRTVEwv2JRuXk1demS8/kVtkNj1fOZMexR4PXlxTbuUzDn2f17BniqjO0vd2BOxr2Ka6UUW7lMTbprpTFMomOt8l7xpbeB5b9AaTxu3w6imfQef3vHWNlids4Vvt+HN4hMbzgNs06LeXpnJOvp49CpEdl5J06J5NsGtzE9c9uR8ObDx32+PeaXuaz5cvFvHgM9jKX2fTOg86x5KvT5vVF5pqit/JdtXM+pc3DT9ys2Pne5aJW2T37neT5vW7d4/BnfGd35vxHceI+183QN/ngAACSAAAAAAAN31fvfI7OTmrE+X7ewdP5fv3vvIZdaonDVhvf6r1x3l2Ac20/qHPNnBqVjZLdmoeXZ/FScDRl8fDGeH2+SlvNReprNmm7QmimqIlNNVb3/AFeK/izd0430zm3D7WoVXrPf4BEzD1+RE/RfIdhyPG7Whxf3zf0spqWxcPw5aoiehzQmExJObwnprfrWC8G6Uvyzz+/yZsPni7RalEVRMQQTTV60ZbYdHoy03CnSqIbrOkjO+Dx7wYrJemrJjqpiLVoproKImImiiumJopmIs67yPbNDe2zC7Dz3DuNB+m+L7XO1bsPGu6ZKcE738/72aG6ZzCHU+b9V5Gt5+m8yy0GK6lyqY3vD7tyitrUVDvHip8WSnL+k4G1iy6nvGj77TJq+a1Dpc113Ttn1aYr9flrpk9VBTL5btq9n1Lm36fuFo9+h7/q5R1e7XNvDtmtvKeo2fh/c8Ly8nze9nj915UJqAAAAAAAAAmM5TJvnRfP6fC+wsas8XpuJt2xaF7PQ8Lf69K9s03Xa+bbnjnNRbtYcuG1bZLezra3e3q7E8mw3WucZsWl4fP4uLYTxZjxY7eC377NLeG36bUWsUXqK2tzMRNV2zcpk6VjcZ0Tg9vglMx3uGiYmAMx3L52+guX1OW9fzNi0/O2FuUb/ADokvjLtqJJiYqQNx6bwLO4su91c76JF+dWPTY2NW3FVNoiKoRBBMe/O2rqntynlmPN598ovTF5OF6TTNMwiESpmkppmmJimaUxExVTRVEW3q9zr6C0uhznoup79r7fNd34t0uJ07yatZmPof582/Ysuv6OHd65ZMaxVnvFXJ0rj/TtXyYd45X0PmEW9dmzET1TlnQ8BfDufJeh4OY17o2O98xqu8efFTGmeSm5W1cXbNMlKmi+Or02PRfHVtepdLvX2bNepy4bVF7Uk+7TPDi9Lo98yfKupeD9TyjmH0188ek4+LHb4xMAAAAAAkgAFXaee9y896CvGZTmfJ3/L68Te935HLVYa7lwbRkNay1o2LM6Zfvj36jUbiN02Tn29475uJnV2fJzDo2gbeppGO2ejPr6Z4txw0W1/xZrF4cuOsevz48vnovWq2txVETFyiqt/T1zj+d5vRxOA7Lxu1aRv6EoknM4WqmTaOi8a8mHY+rfnLM/RCvx/Gyazs61amUTASCQivc9IzNb57Xek84tHnorpyY4gQB69k0+LV6FGo7bmwzTVFq0pgimYgiaUxTVREqJpIpmImKZiJpprohG86NNbdDzfvs48t/S8futMs823bH6+5oPcPm/fL4t3479NfJZl/H6sPE5b6L+bfpjJg4LrOz6em9dte61dn6HhvYaRibuJVyfTdF29Pn55tnPEecGUi/6ltbqm4rXcp6Lkp590onLiyLyc8mMjqVvF4slyxarw5sn3f586Bw+/1zmPSbHmuh8wRmsL77ygXxgAAAAAAK6doxZ+kbbar8J6/DcuymF9d530PPT1+T6rviuzTM5bW8hZkasTVamyejC+29Nm3DnmTvi6xc5lGLJtGvY69nwbFtNrO6m3p3Lu88dz4Of4jY8NLE+f3+bDm8lr02qXsU3aK2pTEWqv2LuPJu/k17dOP2OTxuel9TlBmwzNIlElW46bFMn1T8rdO2rFl4NVtmp7GtEVQQACchjqYnuvINp1aVFFdGTFETAiYExA3nRclem4266M+BE0iJoiZQFFVuEUTEWpiYiaYQRE0xMUTTFsl9D/ADL0fHejctu+aDt/Mu/fNdbeHH9Xxh0Hmem73E8+1/vfL07zzncuWHaeV2Oqw5Hu/szUrOY4V1qHKNj9O8Ho1bbsEnA6bes2p5JK2z+axmxWjQfTV0q1PFmq5y471OM5/E3vFbxeO80wx5JuLpf9GP8ARi2e67ByPrPhvU86479P/PHe5OGHf4gAAAAAAFfbuZ90896G7r+f5Fy97Bxbp9v5G7V56rY/TX5rkvb68dfmPTV5JmuayOve7JTM3MJVfHsd/X/bfH7/AG4D2TXrm18TyeLL0PmNGGyY/HhM1jpjC+TK47Dm8dn02MeSxbvW8eS0qmt6aoqra96vJfw7HYuQdd0Hnb/OI9Fjr8iBMAVRFZOz6upk7lpryae7pLe9P2dbxpjNrwbHE61e2xE5HUlrJjQvWrYZzDyslMTMTSTmcLtdqZWKqdjXilTEyUxNSmCbVdJbVItTE0wpsWNfpfaJ8SXqprpRTMURbteO5R9U478/yOR0aJ2fx3PPE/PtMRenWNoweCpfm1HbuMS8vT9Q6vWaOF7lk5c92bXewI23kXQ+WRbpWk9C4OiaaJtFmpNZ3DaNd3DNi82QuXL0jX/Bp1ZmzR4MWQK3XqrJVbCu95qk5XuXz90Xh93rXMOm+DzPR+ZYyWN9/wCUC1AAAAEwExla5Oo7x5fV4P2OG4tt+m+p4E00R2OPXVbm1b1zz3D03fJcRfWEx7vTjPXaPZV5JtTKe3Cey9Pbfx1dq5a/gLs1yngs+KJ9tOOorbI+W1MW8nn9fnx381u/bpksxXFMlNUTW1y9Yu4s257ryP6F0N35Pu7j4tvW1SJbWnAFVIqpkdJ550vl/N6+e9es39rTyWF2hS+q975D1rLr8jsX7OfDZouU3pkdt1HcMmOkpyUs4bPRVrng3GU6Fut2EKZpvRTMRKnz+eJyEWrhVRZxET7/AF6depfa4tVXp5dc2vBUv4rtiaX2enFZK9YpqiFOz6vTE/Ynz/t28Y8nK+j4ngUuycc7Tt0OWcWyWMmNo6zwbu8T4OO9N06J07q2jdfKea71yVPcsdlq06byfb9TVovU37RYXCN63XU9ry4r/OPDjYmfHRYx5ITcra366vCTAAJC7lsPfxbP0Pk+b9H8F6rkHNfoz559VxLI6/IAAAAEkdP5x3/j9rLeHIc+832Oe+db935CVM5MVU0TMXarUzF2uzUi6tyXvX4fRMelZm1fT68dftX3U+ema3rniqPf5KvMTbtUVvdmxRW3reSuJu2qlbWIuUUvTKa3qvWvRiz+r6R4X0bVy575e+q/katoiXT50RMAE5zD9K0upiNJv+fNrVTDY042rV6MWXou9cw2XX2dJsdj5PuaePovWb48tsuLymbEiJvVCCIQRTVRCKVExXaqtQ1CE4Nj0e7EEXKIqTFewYia5DJ6rsdq3ImLVw+M2jWMeSiYmt8l79fuTXMW/P6DYPo75S+ja22j41+s+aROO3PwaNLonFfZ32Gqcl3nRInD9x4/3MtcZ6jxQ3zJaL35Neq63vafnzzXFsdHt8mQmPDFVUtpwvlpQ8UU0sPfFrN2PDMCYmEgkiCU1XLdUW2LuXzn2vzXo9r4R3rRObm4cmPb+VAAAAEm4dp0nd/G+vnhnUeL9DQiiaPSefmaZms1UTMV1W6iuq3VMVTTKLl3z3C/VZm1fRd8tya+qi1ErizEPbappKKIoi1VMRW0qYiblViYt6JsV1tchVjyL9q7jzdC0noHFMb6p+UO9cFx5IlHQ0JhArudF1ehRreW0TW3UnT4UzTMxVEpija9VqxZe2bp8v8A0wn589PZ9WzYcUmnPhhNKJphJTVbRMQIx1vW8ds9h7M0vAiyYEorlsGvZXHzWxctxFtk9Gq5e1MlYei1cFa2LwVtg6b1ml5yGN95kOxce26Y6b49k5hTJtXDvoPDQ1m5iteT0LO6zs5huZdh4Ge3sXN+vmJRpxhehZPyHDaaqb4oyeJyifJUtTDyFLxM5pPn8dFtEJgEgkhVJSqkpqVxN3etGyWl0voDz2Mj4T0XzVjOic7995gNnTAAAe3xbtg2es+6m34T1/MdG9vg9z5OKZjb0pQmsomVU0yVTTUiqaJlXXbqRdm3MxdrsVF6bRFcWx6ItVSpomitqophNUUxWaphFqpoVtersV1t6PR5b2LP0TQMr2jBf5us+a3t6qIzUsLmts9nO7Xox/p5nrb1ulPd8nAExJNVFVoU1UQudo4t76Zez7BzW7it2XkmH+ktjD840bFr2xgtwomkxAnE5XBxbBVU3cWWzX6MmjDUQSEGTxnqmNq17P67fHjpMeUgV5zAJjaqMXkL0p8NuxW1r0ZDCxOc9GO9Ex9c6ZufOcWbkvfude8xm1TrBpmi9h1hHp6lrfgTluF/THIT3b7wT6UTq1vU9yV4bZu2L46fd4KYtn8BkPZE4P0ZO2n06+TCqKkUq4KZqkhcktzXUW12os113Ex6l/Hm6XunJeqeI9ZrHBvp3506/MxY9DwgAAJ7NyH6D4ncyOsbPyrh9PRqJp9x5CELVmaZRIJmJJmmZVTTMxXNFRVNJFc0SVqJJUwXarVRNE0ExCtpiETUpRaqaKqzXVRVW96957+PN7/on5x+kdDZwWh9C0HndC3r2rdA2reD2Tzm1vNb9ni7XmpiYvjhKESCYmU26qSqqis9e+c63LU3NP2zUqNnW+u+I4Tqy3NoqjZ1ITYTTgJxdLSiK2AAEE5DHkbTrtmmYmCLAAMpi7yMxhc9grVopTW9zJYu6j619nm1KmTgfcd55yZerjWyFjtfOdTPFhMDM12LunCN9i2/aFjraNtzFz0J+a7Xt8N6RBWQAEpmFUVkxdi1bdVVRFVdxFmu5clbu3r6fDd9l6J8l696aZY6pz3a/N+k2fjfZtJ5Wfh6Y9v5MAADae3806d5D1lPB+xcL3dSimafR+dITEzCUzEolEylEkzSLiCKlMyqmiSuIEoiFc0VCmYiURMSQiZRMTM0zFq67ddL3b9m/iz+r6Z+Z+76mfC8+63GC/n5XrOBzVTDrcqYRMJhCYEpgiRJEwlVEor2bVtl19rBeSunLhq+ivnXoUxgrvduU3preEqpRRTXBQlKlVBAETABKAA9fm2Sa69RtdqYwfvoxpc8hS8wE+rx5pP1Pr+d+Ya27XyHqvHDCdbwm4IxvJnXU8Wue/IzXPXMzyCJ2fXtw3KVrceO6rDN4BFqoIkSCqYiZmSuK5rcV3ZixXXcRb9FXstHi9N70zFHq9ew2aw3bHLazmLex4s2cxXQtM4Hovd5a/R5LqfMnm2rVfoPkQy4AEx6ot2favD7/Be20Ll+4af67zVumqOly4ipMQSEwiZAJSiSZgSgVKaiUESiUzVRJVTVRExMKzKCZmFZVRVW9VcXa5K/Ta2PX2bX0La4drX2nilmjf0qUxsa9dIRIImCQFUAmYiCJuUenzVvXtGLyuru6jMNvn1VUTZ6Pon5wqie08m+g9AObU3Fq2aa6SiKqUQmBBIAQTATNKHuy+tXLVymHuWosmEBAmESrog2TvHzPKezcWimE5TFTMdx8HHkT6LFMzDK4oVIImEAJmYmYmYqkqVTWa11Ffrs++9PNfv+q9bHt92cvj129t9mzG9S0fq9MmTwO3ebV2eA+bPabnp03Gc/p53T3fN6fuPh/Ucq5f3fhPqvOwOrywGx65v2ru9ZmvH+I9XxbG12/feNtxLNrxF22imuJCBVMTICJSQBMSSiQCYIKqZJhEWERMzExKqKq2VK63q9Fr14s76F493/AFM3zp4/p3zRj41mp0imbbbHO8fXN1DxcwtZZ6Ri9Loz4NpxuInY1PbYsxlwVUlsaqJRHqt1Y83rt+nPa2769G6jzLDn8I6fGTAmaZPV9F/NWQrf6Jq+ZclMb7z3rO+zHzNTsmuTWiKqZiEwAImAQSgT6fKETETMAIJQBEJQCbkrS9SUQRMoEoImYSlEkwqQmJlNdNUqrlFya3PV5/bel7JW9my4cRe3vGZMfh6tzvrVbZrWdzxOrt8s2DWMZuanbdd51j8Oe3rfqxdcinz29fY2jpHHut+O9XR84fTHANnR10en8+A67yTuvI7Ozajt3OfPdjnNu5R7nx9q5TFqe3zUEEwRMSJiohVbKpiqUTFwomqkJERKAJExIQhKJiqa63pru+jHlsXd42vBm4/sfQNxw5Nmxerci1cvUsNyixi29z8WCzGxiw+E6bmJniTpmp72jgITuc6EwTAATdTTIy2C37X3tN6rzne9La0/bedNzQ9OH6nicWfQ49Fnf5sRXRNUwlUCr2eEfQ3LtN7Inj8bRq9qRCAElMgglCABAlAEEwRIBEgAgkgkgkAEzCYmYmU1RUiaoqtWq5TfmK/dYyF8eU7ByzqmbD03Ttv0nBn0PYNJt7ep2PV9O8lL38bb8MX9/gseXHmWLFnHe/appxZfT2XiPXvOej2HjfYud8vY5EPa+UAu/Q3B/oHzvor/ACPrfFtXNgYuWPXeXW6k0RcrlD00q+KaoWoqVEUXrYqpC5arJiSJITMTMTCJrMTBMpRMVTXW8eqjrGvnwvRcFxLVz9P0fWG1r5q/gbl42jYua04M/VtQ1bYsWzrlvpWrZ8GvTDc5uf2jnEam/wBJ0e3sePa1FmMPu8oL4puWkTXQk9G8+fL8ju3tB2jRs+lWojo86vNYKKZOlUc49Gjv7Pq+w7Pj2eUz78f0+NMotSUCZok6PhNT2OWAjfcVNdXi9ZSKYVKRUpkIRICYABAkEHoPO2HOp0F1vOQ4S+lc4fJ0/ZmcPifO/Y9R8p5j6VHzBp32cmPgyfqP5lmPPXTVatVym7MV+mzctX1+vF12pn85p2Sy4+i4vWar4/Zaxyt8h5fHZrb32fPRW02KLdb2rcW6WuRaUyXemcw3zkdzper7Rj/Jdn5ui5b+geNCY2Hu3G+zeV9S5Z0vSs2HR8bmMV6fzdua/bbHjbnthFqz6PKmxNURPpuUVTFXi9vjIn0XTHL1NZproTN5aFdVtWQiUqq2TVXXJN9m8Gxsl7OeHS2uNbl9AWra3O8t6tZ5nXz2F1TXMk9Ejj1je1eu+Xl3otO9V6nka5mr7l4c+prC5b3+OJmPXX4JplRdtWxyJib05PDsdG8excb4XofHER6Py0xETFUQTKkifX4prfpWoYfLaPTwsZLzbeh54vW74qRJMSR6PPCM7axFcr/mm5C09uZNZdCzieQu8+tHz27VzBOEgQmJIn0fRqeP9h6/chrWfvCiZkhIAEkAATAj5++geZo+WblLJS7XZmYvTZrReuee7aPZ6vBdtT238ZetHpeekqpsxWb1u1TE1rKLU2porKbdUXubhp2x6HV7NbmvwnpPm3w53BfQfIBlwdD6tzfpfjfX4nlHRuL9fm++z5au7wcruOi7XmwYLb9X71Mcd1jr/JUeWj2eut8bTsfimMN5fd5aWu3rN0802y1V/wA/phZpu24tNu4rNuquut6Ll304s3n9G8dVwZeHdm2vVudubdr+m6zp7u5atzrC7upu+O1aOlobTe1Gm0bZicTXM00evy59Uhake3yK2zmHo9mPP4omM2sAA9HnVtf3znvowbe56RZqvjqQza0oAgRNJM3MsnCNyzhzB2rmRhYpzcML6PtTLnxVnfryT5ezf0PJxTNdRGlZ7LizcmSmZESFPj9w+ROf/XXyNJVT2o6J0yiuACAlbxZmJ0rCHUKeKYQ+hZ+XMEfYL5I+tiQATxbs/wAso5lNE3rXVTMxXVbqmLtyzWj0rK0X73jvI9NEWpiqiiImuKITMUxEqFMSUzE3M1gsjrb3d/R5PZ8/9bwnU960X3flA2tPrfQtG3nxHstN4/1nk/oeLQR2ONM0zMe3btGuWpumCxNdq5vdubblav0Jxzp/LrV55b9ilvJa3vXU6zZydml/FcuxE264qrear/rxZfL6updS1djknVfDrHK6O7apj55fUYnUdD6Whl8Fdj0HBplTek0TTMREJIEACQgCEyqvnmezxhT204pT9GZk+X6frzLw+LX2b8ZShEoAED6p+X/uWJuXblJRVULXwz9ZfIBb69yL6cl16UwpSExBMBVEUlU+XCmyNY2YkAk1r4p+qflUfbXyh9mSuCAHn+Yuj/MZ6fMiVUCBAiobn9i/Pn0LEgCSx8PfV/yBMK6KrRNUTMVTEzWqqiS4pkruWK5j0U00zCIiE0xCaohExTNKSBPr8fow7HffdjMp889nyDnnT+Ye08wHQ5/a9y1LbfCe10Dl3T+Y+o8/aiqOryUrk1oTBMwlXf8ALdV97xUzHu92Dk6jq+Bosvz5ffV7fDsWCW89dfYdbY1XuXJtF18n1No/Ccrob3RafPlfPeiwXKvofBdnifO89Y1LqaeqQt73Pqp2nd7Rx13rNHzXH1fmIj439P23kU/FOZ+v5PlnN/Rg4Xl+uyc4zW2yYfI3aT5o47suty2n7P8Anb6LgmJImByL5i6/yGQIlAA6F9bcL7tExIJiDhfzz0/mErn218k/aUEhKBr3LPLwI7XheXTLc8FiRcthuX2L85/R0JAiaT5z4juGoy7N9Kco6vABE0HzLyLP4CQAIATHoPqrpHh90TMAKThHAd/0CYVQmKqqKpiZiZiqaZlUpqRNduormiUIiAUplEJmJgiCJm7ZuUyd4y+Eznzz2/NuT9c5H6/zIdPm9w27Udt8H7XnnNelc29V55brt9Tk01UVTCqmuU03IRRVEprt3bc1iaZi0wE3bMw98+G5W2+4DauV83pU0nS5yJg9ub1djnNVYMnfadDq1eh7vFcyO3ofTvRvP6LUJgIkAJgAATQK9Y2TjJ82V2/TL6q6T4vbABRXrJ8k4EkCAB7k/Wu7+f0QAWrunHyTiqkuz/SXLOqQARPhPk/Q/R55RIECYnPn0P1PGZSBEjEZflR8uTb22X1pmYmEga1snGj5rEgAAHQefd/O602oRci2KrVeiHyzYiRMTMTMTMTMTKZgipCYqmmStTIJIpqgghMkQiEJqqouY8ndM5hM38+9tzzkPXuQ+t82HU5nbtv0ndvC+059zXqHL/T8Cimp1uRbi7Qimqm4TfsUTFybVR7vJVQildtLE1FAhVdou0ydC5b0DS+fveFVHR58QCUkAgE9i439ZxPRpkATS+fT6Bn5Ewh9pYr4msH2LgvlaZfR2F4UOu4Pn0mxYbyid/0H6HR3CoiQHGOzfLRysSBADpXNfotPbaiAEcR7d8pnMqqNyl9Z5amYSSRzvolB8YZ/6wk+Z873wcdzXSYNR2X0CuLdJeizCLvyt1n5YTHa+L/U8ulxZRFyKJlV8yfTXxKnFIEgmBAFX138rfayaZohW5FEF3gve/kJOqTTUhMTKZpmYqRVJMSSSgmJVIgqiqS2mkqimqEqBNNymJXab2PN3HMYrK/Pva885H1jk3r/ADIdPm9h3znnQ/Eey0vkfZeM9/jQodri3YtprXesXD0eeaZiqKplTTk7UR4aq7S03Ikim9REq7dVbZf24Tced0ecwdDnLvRey6uf509XTePaXVxFuY6/CgTOV+3vnD6VhIAMZ8PfTPzAUzMSJ9x4G1Z45u7JnD5+q77wEmJEfZ3yb9uQrABa+H/rH45AkCAH2X8k/cETdRSVTaD4b+tPjknuXDfrOXRnmRF+LEHoi1CbkURKtSRUpFU0yRPn0o37mvJuYp9fhqgu/b/yl9aIhbIuxbgwHx19I/NyxFwoZfOGlz0zLHHXfNhRoX0F5U19EWpK5ok83xd9DfO8WVRKAlMxMxNVMykC5alF62pLlEC5NNcotzTBci8nzUenzwVUzE3fV48lh2u4ZHw+/wCf+w5fy3pPNvZ+XDo8/p/TeSda8b6/AcO77wPq82iJjvcGBMTNMlVVCYuVWZMrZ861blVn0JtW61ZpqXK2oqmutp2nWvRq7vk6zkOyY6Y/lPq4ry+vbw80+i8/ETFqDKp+oeiWL8ABBzjCdiqOa5vbhjfdcFKoRMUnGvmvq3K5EDsH01ynp8PQ8tKPY8UnFfnro3OZlMSAAjq30zyTqybk25RXTEnGvnfpHOE3fuT5V+rSYphFyKKiqeScDPtOfhj6sTvkU0ouTakvcM7ZYPiWzvehpiaZJiR3jtHPOgK1qKZi5FCXn1bcIhhsvXTK5bCEgCVNQmJJou/MdZ1bERKZmJQJkqiZhEiATcXZii36KJWrlF+F+Vua+aK7cWrU1JWblEImu5E28v485q7/AGH2+T1eC9VxnQ9w0/3XlA29Pc+zcH7t5T1VvgH0JwnPgw9N2j03m6SUQlISimRM1UyiaqBdm3VE3b1i5W9ypdx5bu34P6i1s/r5vluM8np4nT4jv8RCnLhAdj459aJ6LNiiI9Tx0S99PhqPXT5qT00+cXoosHpjA4Q3pyrVzkOMmEq6NyPqv2LaLi3CLkU6an5Z8VUJiQRMCY2A+rc1FCtxaSvXPLrkT8m+OaE/QfZdR2hF6LSYvV+X0Q+Y+Z5PGrMxhyPtPIfK31UIt0zFybJHn+SPrvX4n5Gn1eVYgfTHRfmb6fV80wtEoAgmJgmALlRZeq5DwTf1BO34jifKInb9KqgSEzBEiUzCVcQRVNNRXFuZeiKIRF61dJo9V2Y8Fv10lv1VZo163lcbE2fX5fRW/uzmB2zndfpN+28T6DgOA93h+gePDLhyX0F84fQ3nfRe/jHZuX6mzo9u/T6/yliPRYmsRXKLdymoiJgKqk01Xb1bee56/VizY65tO248vMdg630HBbD3L3Ledv8Ah4xlcb0NbzRMdHlxEwAnL/anzz9CIhRSi7Fuor0HbvitP0Ng+H1J6lhNIGaxNsCCYkRMCO7cN+rojcItLVuxaSucb6/hYfINX1Pmon5By315dPlvNfQ1MuJ9Pz5Fc2plcikV8f698f1thvR5ukH0vRTRNbkUpVYDNcwrPz1AsiRPcOHSfbdvTd5mliPXSeSqMQal82/VHAIvq0A6zyaT7TyPw7siPrqv5UxyPsDw/GfkT9gYf5Vg+lMTwInsuF5mNyw2IpL1umSIriETX7a2x85/34s2pN992DNzV1rIYM/Fa+7+zXz8CtfQXmW4NPTed9bkeSq5Vt6ivK25jHXLdRs1/wANVq4G3TZpb25PXicpjKIiVVE1tf6dy7svH722eL2655br8HtzHv8AxgTE924R1zkdnftF3rX/ADvc4/avef3PjLNNyb46b9mpFpeoKa670WtX7/R8Obn+/dst6exrW6aHoWnu9wwnz7rmbD9A53nHPcO1um46VgcGx7dBtu7wETGfXiJgGXT9P7be8ytVMUzFaio5j83dP0+L4COgZk5O7hmkfOr6jzSPkbK/Xd4+XM79BWpce5B9ifFsWp+2/iL7YQiFqTAJiJTCCqaRIgEghV6bfB4tRxyumLPoj52+nTe7eqYaadHq49hYn6D+aLOjJ8siQESMnt3PoNkw3jkuU01CiqBJBExJMBIQv9H1dnmTsXv0d7h9Xe7+DPwv39l5blx6dTGZ6/HxW39Dy3nvQapnfdHK61FacGaJTCEgkQCIqgnSd0x+3q8Rmw9x4fN0YhK7TbmJu00kREwlEkwIKori17unHO3eb9N7ND3vlHNzc5HtfJgN+0HYtXd7x4vXR4f1vBvPmcH73x0TTTsatdVi8r6bKuLXPba+gNfPa3TAcz4/X2bn2neDaw+zx0x1OQ2XW7uPNeeGu1c3gJgC+JEwQVRNPaOPfWcTtNCL46aZiSAomYKJqIpiREVQUpEIvGifMfYeOVyVfSnzV6z7Mp0jok08L0zMeWfXXDwxcwqcq1LCp6Vd4tgofRF35YwR9a87+cqE7FrolMSRIQAlCFV+J87L5DDm1iN592HPzl0nEo0xes7ukiYl7dizXQeB3uZe/oE8/oaVkdijDn5XpWdwXrPJols6z1Xux87oWM+eT9VEpx5KUco3NRpkx67yWV7Xgtp816SRyupCZITSSQiXm8GXHmJ1jL2jIQnBmggnCZvQdvV5rMPb+JVRMwmJBURTeslc0SmlMxNN6j00vs3W9A6J4/11XCO4/Oe1o+Men86A9fkmLfRvr1nZ/Be35fpPVeV+r89aouU9Tj25qrtWn1U7hjyb5td3kfD9BTzeq11uOhGzpiCYIImEzEyQqvVv5/RsNvW38LNm3saWf+xuF9xtisxK1bdNyCiLkSpprFFPorPI92Eh7Gl6wnr1/wCcNTh9a6/8lW05/XhMxIZbEjerugSbPhfCE0STExCYveut8c2XGUvjZic2GE5elsP6eqbXx+vxrIdYaO/zXIb1Ovn1T35xg2PJ6ZnDlpmZpeBJEzDFcU+gOZdvic/mPR6TznZc9Rd8L7mmU4sixf1nNi45Qe58Oy3o7FzenZyJ5X1CUUvVbjkW7o1alD13k5yGO3Wtuq10z4b26RMY/I8l3dK5jtaeq8tlvD52bDUpqyY6+38b7PxO1lJ17wcPtbjGg4/Ph6dx67q/X5VtFXa4qUyEFSkiu3XCYXbUK1NcTVfj34tjqO0Y7JeD9lr/AAHrPJvU+cgdfkgJgdM6dwvuHkfWePiHeeR7OLXqPU9L5vzXLtavr+htUtcrsYTnu0aRabXnR1eJMIQAESiv31yefYMxY5/V9np0jwQ2HW5o3+bENhyYPqDO++yeany6mbvXxjTz6ax/yLgz6w1j50J7DqukD3eGAShCb8TYjM5DFm1dvWQ18/NJ61kNfY4tHdfJS/FXRef9HnWZidnXp3nSe5cvp2ch7Z8x6aia6cWSeIdj4D3uHSnYu9wfX1iqvyHr6kTo7lM1QIkAEwEwISlEyU896JyXp83TNo1XpPovPdEmmrxXsgHOOjcY6vL1raPV1bpc636DzPpJEWWY47vaFzVz13kwy43WuT964/YyCJ8v6dMITwXvOr9TmccdM8Pd4Wgtv8OfBr7I+LPhoqpWqJmqYkAmYkmYqmKbtu+W4qkuWLthNduIib1dmqJ9Oy6v0fm9je/TRR4z0HF9O9vi9948M2uAB6foH527JxO7uegdBwPC7PKo9NPsvKWt/wAd1LXz+PndPO9Xdt26qO154CIkQmSE7FizY/fa9A53Tpsxa6nJqoiLVQhE7bqMQ6ZpWHhMEABHSdfY5x6+8XuP2OJZDrzW2OY5DfpwZ9SyOda+fxeqpgz0qlbQkImSmKiWsbPRmw/OzI433HiPf3vlfV/NekpmXG7Ak1Hj2/6D6/yVXdOXdm0N+oef70SEsXxro87tWO4nT1eX2HHcwZ8G/Y7Umxg2PsXEe5cbs1onh9pEwlwTsXDPReentvGvoC1ayfN+jBFHNukzta1NadXZQkWLfHOhoV66et8mkyURIy/cuP8AYPMemmaauN2QAgBCoim3dTHg8GejLTVMXv7Y1+J4X6H5j2uLo0Ha4soFVVqSpATTJMRVExMSlci9W9ztXMew+b9L6tY2blXNz85g9v5AAABu2k+7BtfRVq36fCew5Zl8/sHpeT7uQ3NG2dW3bro6vFporiYiJlEKvfS+O9fQMpzenir2r4JNdiaepyYoqptWEwmImIIlCmKoTTFUERJNXeuU9l856GYlwe6kBMIJITBBExU81dq3ZhW8xIiKvNavEsRco934brO5eH3eK9nI19gjx2rxfDVR7vw3SugYPO+O9hI09tMQc059ncH7bxYberEghJvnTPn23yOt3nwcTY8nXvDy5nwbdqMxv6O1dh0HfvL+nkczpyIgAiop897iW/ozgZev8kF6ARIe7ufz7muZ0+4ValnfNekyC1cwZ5UzW0wTEqZJQJmlCqIE+T02clPn4e98GkQLhQVCPXZIqVnmuU1Ra56rHsxbG9dBwmd8V66n577NwTs8akeh4IAACYHZ904t2fxvsMfz7pnKM7BeX1eX03mLVFyjLr0xUmKdgzOxcvq+Zo+Pi2VxFu30uVdt0xkx1TbSrUSXqaJRNNcFEVRE0xMRMRIiJriep7r5fV4f2yTX2AhRiMNyTt8bsWN5e6fM37Ham2MGa8HjZ8E7lpvT9Xb36KavG+vAantnLN/Q0bNYToPqPM9LmJ8R7WUSRqO3cs39DRvZ4tz9X5fq9dNXhvbBEvD7dN2cHJ4ifb+ICQgkCJAESBHrrPY89bq8L7qqbc48la2LkUQVqYTVy3p9vc1fn1m8J7HxgXoAABCEK/Z4VbZ/IajGHNv+R5ewbHYPfxCcGfv3p+eOo8/o7uiOP2alIq13YOfbulz1E+y8VNVFUwqokmuiS9YqpTcUomKqr1b29kxPROb19y9Vunx3e5ZznI473fkQ2dUAAAD0d++eep8ftdG0zcvJ5vvcV82Xx3svL+Om9ez6vm6H6dT5nUyWr02t/nRYvefY1aaaqZiFyS0qkoVUiYqKaqRKBETETAG1ar1vQ39uk8b7CUSCxNeS6lfse68QTGbECBKXeuNd08/35mY896CQiOC9m4P6Hz7s/IO/5KXZifNekTEojhHaOBeg4EdY5R3vZwe6qmPL+nrm2hXyjqfBu1x/APT+YkAABEwJ9MW8rM4us2tr1Ppurt7zMT472coE+X08w29Tp063smHMQx5phBjeK991Pr8fkyJ9N5UJPf4OyaW9z319Xjidzmvr6BOLNpmodg4lv6GJg73npiQEHaeW9q4ffuDz3o0xBPHOucG7vBolPovNQuVItzeqmLN6rIJ8VHv8cTbqmqLeyqqcOf09X0jovmPTV6huPEcNdRHsvJgAAAAMzhppk+k7un7j4X2nPNO67zXu87x7bZ1fZw+byrHU4dS2yYvX5osIuW7lsuzesF6x6fORRVdPPeouzFimqkRVCYTTCEi/3vl3VvM+lqJ4naiQaltnJuhz9MRV7DyNM+7K4s2tt5yutscxq6/l9Xa13dEef9DUhgzVQpNF5hm8J7Txm29f0LdvPeivLLm9O9FsafyncNP9h43Odp510Pjd2SOV15mkYriXSOb+o8kHV5IAF3qWldd4XocH7shPG7lm8jDlt8E6zyP0fmHbuQdxx5bsUT5/0lU0SieIdk4P3fPe7tPB9l3NHsC3V5b18ohMg5ppHfuN+l8rhJR2OJ6e8ck675306UcTuyiBzTpja1OdezeJzYtQ9OysWXC+rIMWam5TOLLKEJRBrnJto1f1/jJrpnoc2ubYu1WqT0ZDETWch46KFrlVuulr/p8+06vQ33O2PR4r02H+f+gc69X5sOtygAAACYANp7Z829x876PYtK3bB8jrcxszY9f5WPJ6PHsal+3b9NqeOn0+ear85eLeHyZ7yVthovUXx0+uPTFsWmi1KlMly3coTTTVTAeyJ6ptFu54X3MzTGHNUoiV3UdpjNiweU9BMzSxZKkIlEJmUSJpFekZLkHa4dmWT9F53ruTpnxHvZmlS8wxV8fIPDF/2/g+s7DaueL95UpYs0opmOTa3fse28Cir03x+Scz7cWbWW6ZHBsV7vZveZ9ZKmdfZqpDnWi5rCey8Lt3UNO3Hz3qaphz+lKKYjWeUbxo/rPGxJ0OZ0Te+B9g876fOqZ4velAqx2QWpwjzdW5P6/xO69M5H13h+hmYcrsSgJgSgAACCqJIjWbnKexxLMxPpPKiCZVCqJF574ti16Ima7t3HndV0vqfnvR3vN6+a8rNzjxzHuvHhNQAAAAAG5abdw7H0fThtg8P7LmOu9Y5p6Pj4XxZvH9jieK/d9t8GJ8+Wx8xd9vhuJzHn880vZq8XtvjydXhnHljFZzFXpZp9nnmq3XBbgG66X17ndPYZpnyXsJQJmIKoRESEiCSJSixNfQ17W9vU6BoWleDtcSumHX4rd9I6zz+ns00z5T2UokaVunKujzNV2XWd+9B5voE0T5D3FQgiYMJ6sgz4Ld2JxZQiwCYmAE4jJcj3+bgT3+p8d1vJ0V+L+gTExjuhatHI8JXR7X58GTGyGPVt3b08u6l5H20RFGruXXm896e/m+4+Xd0eS9g5Na7vmu+OX7H5/0u3Nes4c+0NQ898W7uf8AnyY+kOY2L06rTyezfH16ON4/Lj69pOnVdDmIl0eWJE3BXXF88VGSsrReueas3lj0Uyeq9Oe1OjtO0WPR470Pm+euh8t9NwIHa4wAAAAAAAG6dk+a+4+c9JsfOOkYnl9Ll+Ny2O9b5ry5THZHPp1YTM4m1LdyzetSqZJ8dy7TNblHs9VbeXE7NhInyRci1aK7IiaJT7u2886H5n1VQ5HZmaRUihW7GGwOzq7u5bg93R7NhuOxu6XScLp7c0cti4jb1JhN6QASV9y5X1nz/p6kTxPQSEU8N61xv0HmXQOfbD0Ob1mpV5D3KYAlEAiRIRBIIpmK7Wu886PKy2qnpPKNv1HLUydjnkHk4voO22eHWcmHtmB5g2dYOrxQCQ9vhVtetQRMEwmJETEpiQQEgACEhICZImqsopyNgszMJ9GWwGZpeK8Z6D0+H0eqLY29V6seajqerdG856O95PVyvn30XwzHuPIBaoAAAAAAADadWqx5vpOrTtz8N7LnWkdv453+XjK6PP2uF6rflpvh9V3wVWjJsbWeyPPXNcvmsDlsWb0Yb24081q7YyYqbXstnji/7636XmqavEe+IUySQmeM9T4j3fOwO95wJJCJBAJAAIdD3fEZjyHu0xVq7kII0fney6167w4nb0d+3rg3s5Pb7fVy3087q9Kjl3hvj6/TxXx5sHbfFxtmwdX8fNGbX3/x6Yy4dmwXlnY1kV0ZcMTV6TyMt70a03G+aO3+8c6dKk5o6Vblzp0C1DRG4eaWrs95IYx6LBBUUpSRIAAAJkhJETIJkKqCqq0MhZm8mzb9No82Q9GXpfWZ2fERPlzN7P4dnAX9g9fP6mby9NXlevhuBbdpXrvOB1eUAAAAAAAAABmu6/OPT+J3ukabuMed7fBvHtes+y8v5Kb1Gzo24mLURMSTBFy95R748Q9tPjkydvxyejd9C7DzOvlppny3r5QJCNF5xncF7Hw5Lb00SBJCRAAAHo86s9Yx3OHO6u8+DVYz6+dx/jZ9aExkxyCFUkMhtRok9k2Y+dX1bsyPjHN/Z3pPkbM/T1R88ZfuI5Hl+iDT8lnZPH6KrJfYjEG3Oe486k49jzuL5+8B9Ix8yeM+p6flPzH1rHyHZPsKfjeg+x/P8gD6txnzPUfQWH41fOjYbW75Rru3e+XJLPacYconoHhNObHhjyyIIkVx7DzW/XaLfqrvp9921ax5vPX4fRbHtFzEMWa94sdRkx7Fk9ZyOrvbDu+Mz/lfSTqeycEzamHpPYeUAAAAAAAAAAAeryot37O8S7R4z2GL473nQtjFza37PN6fzfmi5byYIStERJESIRMAgAzfYdJ3jy/sEnL6wDw+7StnU5rB7LwsiQEgEkAiSBJMElMhCrYTXJ6/vZ8zZb692BHypufexzPbs9BRVOPMjOk65E9ZjhWAi30m+TMHW32FhvkSiLfUGH+d5O34TlNJv2F1mJjI+CmC3FymYpiqEUzMTERVBSmJiJJgCEwIkQmEABJMSImEK6agmBcoqIiu2V3/ADSXrc0Fy7b90xfs+6YtgvZ6aphVSiY9lGXxZ7We8O/8L0HprjX/AD+/pfOLtn3Pkw2NUAAAAAAAAAAACetck9upvfRlnF5rxXrOQa52zj3p+JjLfrt9Xj+aLk3xWVcWrTKUUpghMEVRnseXp+Qpq8V7+UTSxNEquRb3yTu+cok73nUiCQCYkBMTCQjMdMON+/6h31HzB0TsQ17YcXp50aOGahE/UWO+QMLE/WGsfPE1v13W9Iu1yZHGX7mLN5K/Zdpl8FXvvUy46crOPJiWXrWwtGcsK4aMpRkxYunJWsmLwU+u3fF5o9FN6eem/Ralum5E1oiuJihWRbVRMQmJiEpiIqghIhMTAACYlCYCEk1Xb1o8b1+dEX7foKIqplORxvuRlbNmwmZ8atvbVj66ZMvldY3/AJ/Vz+firx/oPPwfaecem4QdviAAAAACSAAAAAAAAbT2j5v6Tw/QdP0zcXnuxwa1uuoeu874qfb5dvQ892j1Xx+Oa6LUUXbSIiYHSOc9u5Pb9NdM+a9ZM+bQ9nT3DnOu0+h8xEp6HMEzESJJCJmFKvoCee5D6O6CfP8A1RoqO6x8oaZE/WWmfPJPUNNwSJJqi1NVVyt7dd29jy2Lvo2rDn1W903OaubjPq7xmsN+F5fsVykctyXRKldF9W4zFNVq2mg1WzuEp0TH9NizkOH7nSn5twv1bZy1+U7P0lq+WOI0dJ1DZpgafbby4vLTfpvisxci1bcVxNaIrTFCqJiIkrSlKEiExMEiFUooriuYoroqPVf8dya1eWvzpu3/ABIn1WrcFdVtE1qJiZqiut679OZwbmS6p5Mp5D006hmuDbWn4rZ63y4TAAAAAAAAAAAAAAC9ZRPa9w+d+4eT9Z7uUdhxetn4p58x4PWeb8L0+XNq0zRVfHctRVasU3KEbF1bUs75f2OQ1TUdc3ND1+NPa8/ElqkgSAkyWyGs9X17SIn6DwfDIN50y1EJRMWSlMTMxMVTcrei57d6wZueZfreV5fQ0PZve5+5kbms4aK796eP4rYx9vxXGmxi6p4eezkncfJrkzfMebHyt6Omcqhj+mbPK+jamHn2qdt5JlzebJaVTmdKyvHKJx99zHzVXjr9JU8M2XE27TNqz+O3z7gfqDA7eP55t9Q5/wBDWxkX7efBbiuLUtxXExTFSa0qqZiEpimaolVClBSQmmCQVV2rotXLYACUxMTMqomq9T68WxV1nH7n5j0tVFfLNG+v6rMe38mGTCAAAAAAAAAAAAAAAA2fWJx5/o318k6x4v12C5h2/TtvFzbxZDzel8557kUZde3VaqtjuRTSdC5/RGHZoVNjUhIhXBTMiAJEzEglEiYmEzExKqLRNVUWpr2roejt803zZfNxuv7/ADefyaG7lKcIMtg/X6slcJ5NzlXQsJ1mNjDxu50bVOhq4Vbo2cN+ryVS9Lzon0LAv9Q5R6MTvlGP9HE3OTYTrnG+vr3ItNvBdizExcooWpOSx22Yre/bvDleJ0MzT5fTgtoXNvomnpanzPR1/mPX0MXTeo2de1FcWpQmJrAmIExCYIExEVEUzIi7bqJtXKCJipMTKJipVErk38eZvvh6dwfQ3L1OucHdxPHr3m9r5YNzSAAAAAAAAAAAAAAAAAElXUeV39Xd+kI0vd/Ger0XQO8c66+lotn12e/wPJVXayYFJbGiYsBCRAktykpmqU0TJKJQJRJVMTEzXF6Ksp0vU2tF6TkbfA7t61r+oVtu2sa9V1uZM0U7mrcmxExdt24R78zq0YsnTtg4hm+Z0Or1Yz38Tq67zjs+K6ejyeavN3+L6IsxL0TYmJvTYG79J4D2XidLMc26F5NHb4xDz+p4V6LE2rdmyRtPU9F3TzPdu+H28jx5M/iMI9ByNi3Hls1fQFviW/8AM28Hzv6Kw+zh4TRn8H2uVapuU5MdKYmITE1iKkxESREVRKEiK4vItRXSUqqom3NVUXi5Pox5adntdW43cr9VWP8ANdPycM9eD9h5wOnywAAAAAAAAAAAAAAAACYAAPZ2LiOQ0en9E2MBsvjvT8z1Du/N+5ztKs+613OD46rtGTDaqiu1KbV63MUxfszERVJEyQkSiqImCYkqRZM5Sl8dv+x5Tg929b8Wi6O9tWmYmnu8W8sRt6d+356LV9FPnpmPVHmF+LAvvOifVV5oifZ1DkXu09rttfmv+U9NrnKe9827fI01tWU6XP0Oek38OXl7q1/Hfke47Zf182VUOF2ed6X2rB9/jcxp6P4NzT0VtPg2MO17vzronnO/Y4n2/hW/o3qvPHa5PrnyIn1vPcW2XonGbvP3O6czyW8cze+eqOt8q9BxfPTcp2damJTWlKYgqmKYqmVM1UIXbMl23CJuV2q63qqm/jyxnXVeR2mRebzPUo4je1X1fBDscYAAAAAAAAAAAAAAAAAAAAADKdh4XlOd1foWzgdk8j6Xmepd15z2+dpVm/Z7vCsU10ZdeFK1L9kQuW6pVREkCJkmJhMxZM77r7OK6THh856f36trWJ3+f6LNm31+LeosL471Nki5TREq4pFSmSpGdx5MI37P6G/yPMdcu6O7zzK7fOlt+L3w0dyUImRBVbptS88li1MhOKWjK1YvIY8lVVKtii5NZiKImqYqWp0Xeo2MPIsJ3aOpzuCOsa10efptV7yb+h6Lnjk9+e1e5h2O3eLlfUuD2eR+PtvIevysdFdO/o0xVFqzAiuF2Ys27os1vRCxdqvVyUr9zFnt5v09N5HYt5J5fOdRxlqXqODA7XFAAAAAAAAAAAAAAAAAAAAAAAAy3YOE5Pn9X6Ht6xtfkPSaBoPe9K7GjzK1kvN3+D46fTRlwWF2JpbVxNYTKYmZTEyiVdXTNTdt7DRqHA9PlefeWO75u7at29vRuUUxalSlMVIJkzeO+EudG2nmdbmm07VPK6tn0RPP3ZWsHkx7DPP8Lu6XWfNxfxbep2HL8G7Rgz5qIcfq8kxXoxPtPH3qIZsUzSmKqYHq7twHsHF6+fxOTp4Pb4D6PVjfbePyfs1+Kzu+Z5hGvn+gKsNmPKepRpWsb+l1xo+wa+fMaxs8Y8nJNd75iuxyeL17RqvZ41294qslOs5PjHR+D3tF8HYuU9DQ8MXKd/nUyTFVERNZrtylXFdbPRR6MWedo9nQuH3KPTOO4G/c4xZ1v1fBDscYAAAAAAAAAAAAAAAAAAAAAACYAADIdX4x6tLpfR86DvfkPS61zXuWG3cHHbOfxHouF4qfTRn1bEXab0tzURCZTFbouruX8vb57xPS+nV6afQeUroppza0wiayhKV7cNfa07a929HH9D4srar5XXu1YbVsun0PFcqx3S5PQdd1+elzKqTZ1ZglMwiauxca7bx+xk4PO+i4zir1j23iZQvSUCZiCrfdAz2rtdhUPHev5ppvTuY+u8mg3tEhMbf0/gXZfP8AoMhxXuOj4s3O0T6XzOQ23QmtsdoyXBM7y+v13E43YeZ2OW693PWetyOZ1ejzdngdKyfJOm8T0PObXR+d9Hl24uU7OnRFxKiuu5W1F6rI4dnx9DyOyee7q5TrPK2Pbxfy4313ng6fKAAAAAAAAAAAAAAAAAAAAAAAAAAAEl3ofNqtfc+jfdxfqPkvUennPU7eOeGWujaL6Tj42j2WtzQ869TbHbm5suPN7tmnn3E9Pb16I9B5KaYpya8oWiYbFjy4Lb9j9PH9JVftefmdrIW9F1bocffdTxLq8ENrSkiFSCZQJRKZEL3dOU9V896Wvy+jVub0uXzTPs/EyhCVIqUiqu3Ke7XtfzvivceDineuGdvhWIO3womBOya0x5O+2td2Lx/tuLeHqHL/AFXkUG3pECcpilbdM2LiOZ5Pe6fz/cshodbjF/ddI73l+q6xrfTOX2+WMr4OrwrU3rkxavV7rq7uD6X7fR5nsqbXL6RmeUWaPX+bDc0QAAAAAACYAAAAAAAAAACYAAAAAAAAAAAJ2DXlMvctn+dejeb9P0bC5H18Xd4/ie26N3efo9PudXmuheTE8rveDTa6e35mmmqjPpwJiPZmNr0etj8zZt8n0Xv8uq6vt83ZtZpdXz0oZcAEoCYJmYCYEomEzTdi3Q9x8Xp8f7q7zHovFehy7KJ9D5iUISgmUCqIHRdz5n0jy/sLnJOsc5yYtQHpfJwEAZHsHDt25Xb33lnS8dzO7yaLtr1HiCExKBMJhVuOmTh2ux4jTN84fquaZjaNH6PG6TzzbvFqdDAZDPb9jxYbZKqfP9CrD4bknT08trEPVeaDJhAAAAAJgAAAAAAmAAAAAAAAAAAEkAAAAAAAJgXLY3Tp/wA+Zjk9v6Do0jcfMd3Fax0SxmalzXpGg9vn42m/R1eFYpu+q1PFuOR9XK9DEWNNxbGc1K1HX81CWxpwkQAAAAAEzNMk7bqfWud1spVYq837HWOdZLGeq8LI2tIABMADMdb4l2ng+ouabtuv6XS5iPV+FQSlAmqhDqOQ5j0fz3ste0frPPN7l4hE9TgpiQTEpmqtnr89ymboEapvPF9RpfSsv7uYorp1fRy53mer4P0/EuWjs8QJgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAC9uWjsO133OfPm/ec9H0THVZDkbvNNT7thetpcn3WZ2di5hfHrOxp02LlPV85bium+OExMCAEAESAAAJXInY958V/zftfTgsrzPJh8A9D5ACUCUBMAB2Hj3TeV3c3jr1vj+j5MPWfPwkgQmCZ27UasOz1DyWPVwfW85t7bqfe8lFSrJgiqqut6blzY8G1rm0bzsHD7OKzE+bib/qxmscy6mpteh2Y9N50NjUAAAAAAAAAAAAAAAEkAAAAAAAAAAAAAAAAAAAAAAV0DZuhcYvaHW+ivRxnfvOd7ZsFmr2lm5PrfecP19Di9veNU7XIx1Hpt7OlZi7TaltVFq0pTEJhAABEgDcNf3vn9n0VeWnk+hxmk3rPf8gmGfUmASBEgEAnfNC3LS6myz5Y4nqOcWvd4PS+HlE3oiREzEExUtkdy57suh2M5p+1ebW3tRrubZvcbUdk6BneL19e2O7b4nRvWdf53t4t95zrfn9Jw66Do8gAAAAAAAAAAAAAAAAAASQAAAAAAAAAAAAAAAAAAAAAACfX40W3fe+G+vndf6G9HGN34Ha3DxT7NDPoGodss9LV4HR2LUOvzNKoyXj6PM88Xqb4rSuJrQqiYhJEJESz1M2Z9dieL6m9q2V1Dd5QdHiAAAAJiUokhs2s5/W3tmos08j0msYvMYnteWhM5cEJmJpmuqLW6rt6t/Pe2PctDpadnd29PD7WKy1fm5+X1+fV9E3cHRdE07y9/j+vxnT44TUAAAAAAAAAAAAAAABMAAAAAAAAAAAAAAAAAAAAAmAAAAAAABesk7LunJp0uj37J8D2XjdnrdOqZzldC9rW3Vo5Rr3drHS0eBx2PXOjo89p2bC7/P8AEvMuCzF6Vbuz2qud3a4pwMW8Vit1PP0Kk1pSREVCIqgRUITKYVImnL4vI4tjYVDl+gwuJy2N6fBoq9GXMDO8bJpb3LNh6j6uZvaXtORcves3vLr9Z2jFc81Tq6PRdK1qO5yL1iY3OaEwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABMBMAAABfy2CUzdC2vid3n9T6A9vCc/yun1tpWX525m/LXfw31zB9Cp2MXJ6OtU7WDklHVfPktyrCdex29o8xdA8ezqaU2yxlxa02GzbHg2aWjCM7cidenY71batVuHtxZtCdK92HLyfJ9Wyers81vdKuaO9zzMbfOvOM9d6zrZr9WCweXHu3j5dr3S0+sarz6x1edn8NYdDlhkwgAAAAJgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAT7vArfP5vRWDb6rl+KXdPd7r7+CerT2u8XuKZLU2etTzXIa+feKNV9GLJsUYb0Y7++PLXE3Ztwm7Nik9VWPtTGYq1vy3x7hGiY/Li6VRyfF7ODsvh4t5NvW7BiOXUbmrvGFwDc0/f4qWfUmC1AAAAAAAABJAAJgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAJgEwLldhFvU8qLeqfIPY8aJ9ceUj1UWExeooTUJgAAAAAAAAAAAAAAABMAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAD/xAA3EAAABgEDAwIEBAcAAgMBAAAAAQIDBAURBhASEyAwFCEHFTFAFiIyUCMkJTM0NWBBQhc2cID/2gAIAQEAAQUC/wCFJtSgmI8oJrJKgmlkqBUEgwWnXwWnHB+GzH4cH4cIfhwfhwfhxQPTrgOgfIKo5JA6qSkHCfSDaWQx/wDgiW1KCK+QsN0MhYb04GtPsJCKlhAKE0kFHQQ4JIcS8eBxIcCBsJMHCaUHKlhYcoGFBzThBygfSHKyS2FNLR/3bbDjoZpZLgZ02GqGO2EQmkAm0kMdnIhzIG+kgcxogdkwQVcRiB3kYh8+jD8QRh+II4+fxx89jAruMYK3jGCso6gUxowTyTHMhyIZ3wDbSYXEbWHqOO4HtNh6klNhxlxr/s8BqA+8GNOuKDFAw2G4jbYJBFvnAN1JBc5lsO3sZAXqNAXqJ4wu7lKCrCSsHIdUOajGfFkx1FECkOEE2EhARdSkhGoXiDWo0hu7YWETmlgnUmM7m2RhcVKw/RR3A/p1xIehPx/+twI9ZIkiNpsR6lhgJaSnfJEFyG0B65jtB3UZB29kuBc59wGZn90Tikhue+2Gr55AZ1C2YatGHQl5Khkj2wRhTKVCTSx3xJ02tIfiPRz/AOnajuPHF084sRqZhgJaSnc1kQesGWRI1E2kPXcl0OPuOfsZKNIbmvNhi+eQI9604GpbboJRHtjIWwlZTNPsvCTSyY4NJl/0WBGqpEk4mnW0BqI2yWMbGokiRZMMCRqEPWcl4KUav2pD7jYj3j7QjXrLobkocGdjQRiVVMShM0860HGltK/50kmYiUj8gQ6NiOEtEjbOA9LaZKTfoSJFnIfBmZ/uDUp1kRr9xIj2rTwSslbYyJMBmSmbpxSA6ytlX/MkWRDo3pBw6dmMEoJOxqIhKtGY4lXjjgcdW6e2BgYGBj9uJZpEa2fjiJetOht5LhbSYLMlM7TqkBxpbSv+VwIVM/KEKmYigkknZSySJl2ywJVvIkDJnvxHAdMdIdMcAaAaRgY/bciPPejnDvkqDUlDpbS65mUmfQusBSTSf/JQ6x+YcGiajhKCRspZJE28aYEmzfkjOxAkgkhDWRG0/OlhvSLpLTotogeiY5h7RL5CXpufECmsA0A0gy/aEoNYwZdzE12OcK+SoNvpdIYyJ1OxLE6pfhH/AMezHcfVX6eJIaZS0QUokibdNRhKtH5XZgJIIQKrTUmxEaBW04n36mGKpx1xnmOYTJebCb1bAkz9PWgstNm0g0A0jH7JwMMsEoLQa3W0cQhskE4tsKxntjT3Yxwbtt4JcJWykEsrLTyXA/Hcjq/4vAr6N2SIkBqKkZwJtozEKZcPSQZ9hJDTKnV/J41aihgQnzlWZuEt0aikGTEdPRjmvAk2TMQlWU6xVHokGIsdiKI00mjsqCHcNS4bsR5ScAy/YeJkWDyljA+gwRbrQSx0UEHySMdpHgQ7d2MIVk1KLOdpUFmWixonYv8AxUaG7KXW0TccJQSdpEpuOiffLdClGs98BKRW1D9gqTcxaVuDGkXM62fkVrEG0aloceSkW8lt6UvUDJBU6wmBioTyRhCSUEqBKDL6mVz4LGoYkhhbLikgy+/ba5jo8g0ninucXwIz5H3tuKbODemkMvpeSDSShZ0LcgPxnIy/+GrqNySI0NuMkGokixvW2BIlOyV9hAiEGE5NkXti3CjM0rMdpWplRI5daY98pZURUqA5EbbsW2m2gS8jkCWCWCWErCTyI7ymHNUVqZkZaQYP7xqOp0nGFJW37I8D7Zn49MRzlI6q2VErO0uA1MRY07sI/wDg2WHJC6yhSyEoJO0ue1EROuXpfeRCLEdlO/lpWjSxp0SZLsp5CDcVEjpZTyBykNh6Uj5idskFbLHzOSYK0kkCunSDd4kM3EZYZeS4SVirkEZ3EA6+cotj+6TjKHuIJK1mSSSXhWyTgUk0ntnu0Qf5tQ+1LX3KkpZkJdSFoJwrWgCkGg/+Ar6t2cqDWNQ0BRkkWV6lkPPuSF9xBBDSEM2olnZppCUs1qyI/BlB2JEOM2SEViSBsNpnk0hII8DkCUY9jBx2HAqniuBVPIYNm5lw1QJzUgavik7GWQUD34GYbhYP7aK3g/H9A4s3D8Ghiy7qMv6E17oadXCZg2TchI+os6ZExMmK5FX++kQqqJT4ZYSwgSJLcdFjdOST8CSDLSnFu8aiqnP+plbMw+QbShoc8jkH18Z3rGQUppQSvIJQJQSoEoJMG2h1D1MuOtuzRc6cX7gwZbNRS2dcJpCvcx9fs4jBOeA/bueLi5sfdoT/ACtQlmii+6ZScQUuG2cC6yG3CWQm17cxuwrXIK/3tttTiqmiJoJSSCyLC1ahIlzXZi/AQQkaSgeonattNyCVDkFOEhKnXpBusmyExWcKgtqHF+GbEtLwJQSYJQQoJUEmCh818fYyBkOi4aUx3TEdDiAcdT61R21LVDQ4tENDS5TKEn9h9Qhokp7T9g671Fn7iKsyX2OtdTw6EP8An78v6HC/TN9qzavtFRzYkpeSJEZEhFrTrhn+8xorkpyrp24acAzJItLsmg44p1fhSQbEBv5LRWUv1kndo8oNWAWZLqcJTO90Mqy2RjIfjCNL5glDkEKCFBsQj4p/9VGDHWdIM8jb7ZjBuNfYQ0EpzucyaB9Q2300dii5Eto0eDQf+1vU/wBEhfonf6reLNcirhWCJKA42ThXFEbP7xBr3JzkCtbhNhx1LSbS7U8efEQSQ0vVpkOak1A7aSOxhYkL9m0dNGQ/+ZmL/ayM7PxydEaZgEoIMNqDIce6ENRg9kQVKSSeCewzwJcwlp88eP1QlJJT3SHOCRFawD73Mc+7Qn+7uCzTwv0zP9R2MuqYXXWaZBfUKSSyuaMyBlj91raxc5cOGiI2JElEZFjauTVeMg2ICTY0m6XFzsSeA3+d7IyPqUY8DIyMjIfZ6oiyMBKg0eRHFw9whqB7cjIMylNmkyWFLS2n17QcsFGHJLjqPP0VGalqUEIJtPa/IwYZjk4nvdXwT36F/wB9a/6qD+mYX9G7UOqbVW25PBKiUDSSiuqInCUk0n+5VVSucuPGRHbE2c3DbmznJq/IQQLLFbp3ua9gStsjPB7O2RkZDzPVER/kI5BlOCtJPXlGD7ORpBnn7FKTUbUVXMkkku6Q/wAN4yuTPd9A4vmrv0P/AL+yLNZA+kos0mO5JmlVXaEoEolAyyLqj6wUk0H+4VVSuathhMdAn2LcJuVKclueUhRQ/WWeuZn5NkINxaqd9JyIzkZQL2BKGRkOhCspyMjIyEiQyZipeKSifL9JGyD8p+FiOp8NxkkCIkl3zP7m0RX5+51Clp2z3aK/+wzveBAD5f0MY2Ps5cRxchElZKB+4uqUnyUk0H+3VFSqatllLCBZWaISH31yF+Ugkho2Hwb1NKOTY7V6eU188uEgpTakG2rbkMj6k2e2QQIIEdk1BuGqvsJklUt4wf20AuDKloSnwSy/j7Qy/i9in0JByjCH0mlzouHtjt0ceNQyizDgB0s0Xef0ZQSocoyrXm3OYxkXlL1gojSf7ZVVSprkdhLCBZWaYaHnVPr8pBJBtBqM2yqaWU715O1R72B++znSlF6L8621Nr3P2MjzsRBCMiPENR/wobM6wXNXn7Jts3O6K2hccmmiX4FsJdN9k2Q02bhZMgiQsgl1KickkQWtS9ybBJIgrv0ieNRPFliAX51/6TwR/eDq0jJuqs8BDhLLHIXtLzBlj9rqqxU52PHTHbFlYphoddU+vzEEENKwPVWWqpvCPvSlmeYMwZ7E62+TtU6hPAxxHDIb9lkwYZiGsR63Ak2ceGUiU7KX9lgJUae6IpagSSSWxnjtMyIKfbSJK0k32IjqcCYyEhTDZg2UJ8WlP/sLn6IP93GaUtj7cCF71usCxF5YFVZ5CFciUklFe05oP9pgQVzXocVMVoWNgmG286p9fnIIIUDJVlJqeQfp96Q/6i8nis+xmQ7HP5mh0F8scBRYJh6BBUiO9GjJXfEkpFhIlfZKQpJIYddSUZ4xKdNR9zclxfZIeS26SiUSlEkm1893UNcXnUn2OoSgIPkjZZZT4dL/AP2BXuIX98v9OnYwYx2V3vVax/wwhXE6uz5hKuQW2Tibup9Iv9niRVy3q+AiE0J01MRuQ+uS55yCSFfEOZL1NIJlnUq8zN6n2sbBPCWoH92g+CuD0p5tomkTJxtrNXLvgvJ4/QGeBJldUKDB82Vo5giJJbPGZr3bxzeeIJeUhCDM0bGWPDpv2vuX8aH/AJSE5qkkMAyB7ntVf6fWH+FslZoOrsOskvcSGEyG7WtVBe/ZWmlPLqK1MNkSZKYzcuSqW6ex7Y7CB+BI0ax1bK3les1FfnytN6z/AD7lOJRg/vIZG+sE22gn247bfeiW62lx9x7Y0GQg+y+xz3XFYJ1Tn6wy0boVELir2DH9nZz2V4KD/dqViTCT/Otf61HuWAYMYBkMbVHvTav/AMDYhHdNlcCaT6BYQkzGZkRcN79jIsnR1PRSQddJpE+YqW5sYPfHYe2BjsIENHl0oEdandQ3Z/1Helb52N8WHlA/uywIKiWkLVxTLknIX4GmTdEeKnqWDhG4hfBaVE4nY1cSUGnekjaIXFsKaQo91e4wPYIhyHQmis3AWl7Yx+FbIfhacPwvYCspJ0Oy5kqVEiPomsp/kWf0AyGBgGD2pvel1b/gbEMiHMVGciyUvIFzVpmMuNqaX+xUNT1DSniRq4i0mm+riMAwoH4CGO0gkaZPFLHjHCo7F31C99OM5c1D/nGD+66aiS28bY65xlhbHVEuKcZfgixkuRuBJExpBqIQiPAW4lAffTwWfJe7PuzsfsGKefMCNIyEg6ekikU2gimeq+kHtWWKw5dz3gqbJUPUvDruDruDrukCmvpHzWYkJvp6QVkQKeyoddpW6t6VX9D1Wr+Q7MiLlmMy6TifqNQVPMv2GorTmvNNk0gWUwxwHAcQsGMDHeQyDPtIaS/mK/WtihhHZp1n+WtnuvYmDB/bERqMyNJ1zKOCkkoT2yONAabMLWlvay/xPBCURxlr4JlNPcmIraB1yVLC8mrtifohUFhPCNO18IJnwYCZupZDoVJkPA4chwHDNA6cZIM4Q60MgcmKDksA5DI9Q0PTuKJw+mZKSYwRg0GPptyNIblLSOqlwGDFK5ii1IvnAIOtqaTneAn+moUcNaF8yUglpvqv0jv38SKqW9AhoiMiXI4EprI6QNsOFgLBluflIaHd4zL9xTltvjJUzZtBSVZMGD+2rkLU+oiVvLnJNDaeQgrSmTklB5snmpMVUZffCMikSyPoOzDdZYU80XMzXkzGSwEJNYKE4YcQbS6TS0yyEeLU0olWMiUDIPuRiJyyiNBd08oOTHXQs8ixkQKpN9B6DPZjJ3Mw4yIEOM0IrTTstNPCda6ZYNKiHuQPJAtkPAzFXZxWaq6lx5EJko0cTHusEngEeQQrfequMlCqZ/WQR5E2KmUzPhqhP/epSajpKworQdd6aSjKMHGC4+A77Bz3CiBkOIwMA/IQ0/NKFa6qrzbe3bXwW/bmkJ9JqFiXEdiOqIH9tEccQ8nPHZWDMm1KQ2nm4kiTtYoI43ezgnPoHYKQSFKCIJ5fj8EpbUsFCUEkSCjMuSnotBCqUzLB6UDUlskTlTH7eBZsDlnsgI687V6+dvWJ+b6W7NPw/WW+oHjkWWmMSqytymS0rDZK/KYgJNcif057r7RNvfTZf0Qrc/oX1IEKlX9LtyzXxnTYchyidQLysKW0pJoP7ygrDdWksEZ8SgV65IKtDsPiJMRwg+2ZBSApA4DgDQDIHvgH4C2J1NrXzIxxJO+TEGcuC80uNfxJsR2E+ZA+/ieYmipMurhaJnrlzdESSm2On5UKXjwNoNxcaF0El+XaU90GhWOJJftvKWlMfgpJb4MwUB8wxBIyZhpad+hD6FsSSRs2yt9yJFZ03FUtTh2F0hoOyXZCtIclX6tSG1qHVdP8rst9NNdS+1IvncaLk9G1uYXoLPfSLPTZfd672jX+NmTfSu//AAn9IhN+lrKtw1W1r7TCPA44I/ckfXYx/wCdqeQbcex/NWitmGy4w7zSZci1FWdJf3dfDVNkRmCYaEON8wlJQSSDqCE2X7SUcjNkKZCmxwC0hRAyGBxGAYPwENNOcn9Twfy9tRYnCdlRk3kNaDSDB9ldpqW5YvUenmXpWsoDKpespbko9Q2C2U3k1CdNapclO2kMoFjjvacNpZT2eDlogguwfUbkhx8MRVyRGhFGGdzSRg/cOYJwfUSCKE8ZeLSMJLLEt81rsbN6SZ7aOXx1DqRjpXscvxRpXfSRZvLs+VtEkKiSdbsJU5v/AKzRooZHpbmfH6V8E/RlpUh3UK0MipTytbn2sQcZTdPkI+u//ttA/wAaQvlXEeQgVU3kEq5FLjpkM2EM4cj7lKeR0ld6VkLNRnXxEwo2y/dMpr8y43ILjYC44cZ4hZBZBSRgcRxBkDB+AhVvenn2rKHifaUw726bslDUsRPJRDAwG0kpxGlK2vk2Otekp2a647uw50nKzUsFsSaiq1MdnUSqp7AMvE2840I8iQRIf6qtskQlT+W9fG5DghSz8OR/jUdWmOiLZVkK9ps7afc6N5rdrp3ulLT5Za6zqvltltopObW0PlZZDP8AWNIbMMnIe1rIJrZKjQu1IlTC+uMDTTOHZsj1MnTaed7anysEMmteoVEh3/3b+uxj/wBtoH+M4f8ALIMIUIvVN+FI5EL2t9UyosH9xQ1/XdSniRngqosuo907L+hsdQyhpDtcShIYJsSSCkBTYU2DbHAcQsH4SBHgSHOui0L1Rdsd9UZ6JwtoCkGQMgSTUKimZqmrrVLsxw/fvrp51zidbrktW9W1GQZA/DDgGFupaS3LaeHqmeSZrCiOUyRSZSHEojOuBquINRiZPxfUSJf9C0uRyqaobf0xZatp/llkI6+g/wDEFguoZiOf4s0tjG2iE5mzVc5g0ZJxJsYvo5o0kx1rrUEg5Nps7/MU6f1KPBzf6RSIbU4NMN8Lae7/ADunWeUxxfVMwk8GXvsYP9RbQf8AGX/ZIQYr056LFahNL/gvNK5JWnmm/rvTP/bxI5yX4MZMZkSHOa4qxGfLjzIdUga+QQWN7RsOte/RyFxwtkKaCm8BQWD8JAjFa56iksiOJZOJIldulpxtnqSL6aywNM07kmRqe9bfV4GWlPLh19swqsjO2GknUmhfgjvqjrekOvKXIccQD3IMFiP5a6QmRC0Qf8lHtXqeRxjaspZmmLGCFFgal/ntJGNJ2nyu11pVfL7IaESH/wC8K6UcCbrOITcjI0oj09c4s3XB9BWL6mnCktpFHXlKsr6f6ict9ay0mXFTrnUdJs62ltMRkAiH0BLzsf6y2r/eMv8AtVta7YLaYbjNBRZEKR015FlETKYksHHe+209X8EEH3SZbbcMzju4DcvATMyPUhh7kaP07TfzBbOQUXIVE9nYgdZ4h4GQWkGQ4jAPvIaZc5takY/hn3Vsj08y0iOWlSZYFK2Vfp+yeQ7M8NPqObTrnExcw4eo02CXaetsGz8cZDbjsmKbTrDPWdahNN/YUFwliy1hSuFY6Km+kstQqkVgVqOJNKO3Bs9NL0rEcC9IywuskXOmzprBsaPYcjV6mXcky4Ok4OCrfSnpXg+2qv0qUMdFpIPpENNKKRWmpRKgEVRp5RmtTbKnV1jZw9PUUFM2ct435ktRvSEp7OWB9TLaB7RokM5JIabYa2nzURCalOJkw3idb+o1LX/bVkM5kllsmkCwk9RxBhDuAUgIfHWEd0R5JGXIgt0iDp8g21yCIxEOikS4xEmYn3dQFJC0g0jiDSDB9xCnlejsbaH1Yx9+nrQ+iXyjUrVvVPwqVSTSe5F+TuqbA4EqdHbYdrYK6apPwRWEPrKsaEivDcJ5t76fZYyI7q73TumZzL9pHm9SbdaffrJ2h+fpXUux3fWPJGmb12JaavKVVTaCc+9p759YGR3U0wqzlKGlJynZMqscO71m8XqN9IrxIaq1Tb7VM3qvwqlTokPMw2p6jiabitlUUk5w4UEEsyH6iPci2zgUkHnG+u9jbFHPlkOyMihsDMJPJS2CkMzopxJP2Ze50Vf6aOJskozJLyErwOqEuhpwEsNOhuTxBTTHq8htzkI6d3/ducj3dSDbC2gbYNAUD7yCRBl+sg3MT0s3urZyoMqxdXCsaPWqkCyoq7UrNrTyaeRtnwZFRZWMNN4wzGtj74zxMOMzm3vttMWny+w1PBVS2+r0m5HUf4r0/o+ydTdSGIrt5YOFBknIiKENxvVWna9PpdGkUcwttpI/hCLK9HKeix0Sb6R1LXmMmYWni3pDkd2ko8AJZNT8yxUoGxxZnQkT7SQ9660tHutI2Y/tuoIkAgW1fTdIIUYLaxuuqMEgnXzXtFeOO9AkE80NS1/NH2dJC9VJQnik/YWcrryCVgEscwlYaWErBO4BPgnwl0MO+8JzkW0pzimV+YKZ5D0ph6NgLbDhBYMH3kK6acN+2hFYRDLB9zjvVaFJfvVruYWpoF3TvUs3xV9nLrnb1Tc+rV4Yso2FZz48+KrkN6lpaRpcuo0xYqqrLUNd8us9fM9GTeoK+pRo6wdh3WoGkyIqFYVPLCwmOt5dooajZdTO/wDCRI/RoxhxhmXg4lpLwuuY68mmX6y+n2JV8Nr+m1CvptGLLSkZRsgjWqsriiH9R9B1UtIsrVc4ZS0Trpu7p9xRy+kpKuRSGieasohw5P2JFk6SF6WOLSV6aNkZHIcgSg0sJWDcCXQhwJcDCxFkcAiYkwqYghKk8wZ8jiw+qaK9ohPrUkiU1wDoWDB7H3EKidwF9U48VHduVj9rDa1RUKSaVeGG+3HkXqGbmo+viYdJ9H2ZiunuVkwyRJc1PA+X21JZtSBrCtW/RaTsEsybysOon1bbz1lbW8Ruy1PpVURUjD8YvrpuImExZ2a582HIavq2XBXBkl7OVFJ80c1DeJYRUrOw06sz549HV6erjr65+Om0vr+R1pS/ptCLLJo/gkQaYW+uHARCIgkSJDUVqZOdsHFLJslqNZ7ISahkkhlw2l1sknmhqSB1GvsaSH6mUhPEvoLSX6iTkZGRkEYQoEsGsEsNrCTDa8BEnATLwFSzByDMNLydcRdMP/27AvzPJC0hRAyBlsfaQSYr7PJXNT6U/Dpa9XCftm4Ltxd6fl0j3h0hJW1cGWPFBNXqPKfg0vefLXr6tK0rXVqUzVyysKu+opNI/LR+JtM6DhdSXdS/mFrpzVXpCutMESKGpO2sdU2pOq+ghzHYMmSmPdw4Oln1TLy9TFQeVHpd4Wld/Wo9YlKoNn65iEpNbULcNxSvptXp5MrRiPFjuSlxIrcVvG0uc1BQ8+7NdccJsGZqPZKOQ5+xAhSS+Ckq5FIa6rdnF9JK85Fk6SF6aMLaX6aNkZGRkZ2SYJQyCUGlgljqgngh3INwdQNL96yURJ6iTE2UlKJR8g8kOJCiBgyB9xAgkVcX11Jb1RwV+BJ4OxnqnRWNQKXU3NAlhHe00t5bLRUUfxQGuov6p8y3EoCFk4nsyNJagNw9W0imXdFqzUUtsi6j1dM5p+VHR8g0ftpK7dJ+4dWxMNalKVtoqKtyxkEZj3yn61chbFhKjNsT725XPeroCYdRqGVzcB/QtqhOWVt80MRkMNpSMCxs0QwfNxbjvAfXdKApfLbIJYZfNpyukE80NTweTfnponqZSE8En7C4l+oldmdiGRyBKCFgnBzHMIWOY5htzAalcAmxUHZpqC5HIKXkL9wtIUWx95AhoqRkXal101wk8vBkUn8Wvorr5cuZCjPSZMV2I5vDp35sNMauiBy0kH2JaccWqtPgaFJ7q8uLHmk8+n9Qy4RR2nTUrsacUy7V2TFtCrKs6iZXTmpMzS9+9Jk62Q+8z9dtIVqWGU6lT82tKCNbMOsuMrpqd+5l3FsxQQai4W29f1wJJmqvpWqdq7dVNrKOMRuynDYireNxXMEeQQMUZZYIgRAiFjc4CUYDrvYSSQFKNR9hDT8zAI8lMZJ9mbHOLJ82n4fRjizlelimYz2kCMZGRkEochyBKCFjmOQSsdYJeC3QpwdUdQZyFkFED7yBDTsr0ltrSr6yDBsrS14KBOIi/wBbSvVsRrBDyJ1auGZkMAoPodCmD3gqJMj67ZC20ukusTk608KhvISlBuKSgkJ8SpTSVkZKLaUZkzsSzIyUS09lLa/LJTLxPItIfSt6+Mb+r41ow5dah0i3MTWVTtnZawtERWciounql1+FX30M7CPRVEsnnttPyl2KIUOBRC6u12TkRrOnKSCSa3U76m29kfUgoaeTmMlIVxQixtVSgkuIce5b/UYJgGZqPtIQ3+g/Ce6jZ+41NDwry18c5MphHTbF/L6r4PuyM7ZBGCMZGQlQJQyOY6gS4FrBrBqHMdQcgYPwt+wae+c0bNKcu7vnEnL8Cf5CqCTNJvK6q62zJpNlVnHKrabN6xkrVpAHvCTzkd2Qfc7IbZCJbbgJZK2deQ0H5fNAQ4psIXzQFESkuxVI3ak8RnPYY0XNORFuqhNm1QL5nJnufNCu3W4Fq8UNqzU65YBhhyS8qOzp2nbjxtQwUsuwXn4auvV1rGnI8+KdjWwI5ypsJxS0FYc7fUL5uWAwGi/OQV9NNpzGkPtRGp1k7YL9kk45z3Qg3FGpLHhIUUzk3kWsUpMdxBoX5NNw/wAolvlHYccN1Z92QQzuRhJ7ZBGEmOQ5DkEqBmDMZGRyHMctz7iCBTNohx3oCYInOdWX31cX1Ei8k/l7Km2OGq3rCaiXcI5EJXsD3ri/i+LOzh4RnZt5TIOeoGs1HspJoOErKN5bW5Bt/A+u9NPVW2NvYlVJsMKriGiJX83qiUquU7GrdWRbSpk1D2mqxFLX3tqqxkwZr1e+n02pYMGEzp+vubZdi9pid+WJUm3qHmTLUWKTNpLWb8naMWXkhz6U9i3AiSpLsx08IClmrdllTxuOklPhIVUjoSo6+SFFyTqCH6eV42kG65AYJhgaklYR5SMZ3IxkZGQShkKMZ3yMjkM96RTR/V2dXZdfWOoHeEY+9ttTq2m266K+8qQ9201wqIq5rZLjsaAvVVU42bajBiA3xZ8i1cC+vc1FbJEz/IQ6tAbdS6Wy080LI0K3S6pAS9z3hOp1Hpqsfbr6HWWnijK0RDREhIvjl2MRudAtY00plNeXLyTsq0mRVVT9rIiM12mkGpu2jSojkN6jYfetZJtwY1XaHY6jmyER67eF7yUg08tlLJAM+W7Mfml6RzLxo9jp5XWZGoofXi+Ogi9aSksEpXErGT6qZ5CBdhGMjIyMjIUD2PsyM9xDSKc2lRZHF1DqX+x3JSazr4BREW03rr7669lVxxNey3ZeqNPJsiMjI48U31fTxrlNICp5YccU6fdFXzZcUbjgJRpNl4ni2caS6DiLI3GFNFuy4ZmNByOL2vVk3S6Mu/mcbWOa+jrq9+zlX05EJDPUo6PT0yLbo/DRRDdt4lW3ayVGmtsnax30UbUUJEONp+FCfRbRdNxnvnto6Sajeu/zC2UviPruhhLSHnlPq8iRRSOK0nyKQjm3PY9PK8Wno3SjC4lemieYtyBDPZkH4s9hDRxfzrnsuJa/OKZRcVdjTS3lwYCIhW0/pF4CH0Dcp+w03Y0repo71XJrm/FKlEjwpQagxEWoOt9JexKNJpmEEupc3fY6prQpveN+saWeU3faniJnxvh+w781ZlVuo585uPpCu05HK4n6gsl2ljVabZrIz+rqmYmfRMvFKUbkmn05LuVNzquhr7qOVxA01IU0uugpgp1JHJqs3rf85IWvfGR00V4WtTq/KQiuG09Dc6jZ+41NF4ueGK1134zZNtDUcnnI8xd+diMH487pGlXiat9QwTr7eDMXBfmG2qTvDrnZYjxWoyLGcUNBmaj8WhbEmZFT/Tbuu1YTMifpqJOKbXSK93vmSOn2cTIcFcOyE5xcyJ/93sSfFTcpKiyDBngLjEoKQpBsK4rEWQqJJmtesgxTZqdP6cjyH7GLOgaqh2qEacptO6dZp42qpU5ywFDqZ2sVMoqt9d9qFTSJEp2U5p2dziKgl6i9nlDg6uXivB7QXSYmWERyI5nZplx9w3W64vr5yIJIU0jkyLyL6iIfh07G6kkiwHFcESnjkSPty8pbEITxsSdV0XzeKsjSe0WrkSg1Tx4iW5ByDlzUQmnXFPL8ceQqM9qeaUiZq5CZiNPapk0ykrhXkG2qnauR3T2z5mDbWlDLhNLmS23UdsbJviZ/f7mpJElKiWmWfuh1aA6pLsf6BpRrSf6YUpDkPWqlKjSlRqhjRtUdcmkegakc1ZOuIM+PqGBcN32mXacado/lAe1a2m0caYtmJVY1KdZRKhzWIKotXqlDnHUscnKgHuxOkx0/NpJj5jkLsFGjx4GBgYBEEpBEKp3g8hWUvJ5onsmxL8Gno/Sii8kdCF+zkECvsv6Peafh2YTpqEybkaDDEi9SQjwXZZz5bcFt15b7nlN41RojpStMChvF1T7TrF9DkNKjvdi1k2l2YpzZSjV4IZkl519LKnl9R3viLDkcnTMuJ7N55E5yKhaJdLYqNythRpFxY6sl+lhrIq+NWXTVwiz0ZPjSIUBFZS3t85OkEaJaKyyfo5k6ti6hjQK/pnq31qTQbdvAuMvI/wDHnLbG2BxHAEgEQJIZ/hrir5IGpo3Tkd7LfVdiN9Nkakkc3/2dIIVUdLlDFU5CatbQobSG5Nk9Dp24In2bcMlG5MekJS356Ff8VXsYo7tdbM1ZFI17uPttia8l3yZz4WM9VbxNEtXJWyVcT5cjqlE1p7RtsuwejU0Koe00yqxsdR2Prrg0t6kg1uoZ8Ri61c9ZNhJmhRKRLbiuL03Cl6tccZodTHEJiHGhxU81LlNHHk+chgYBECSEpHAEgcAlAQwahWrMttSR+rC76JjqzElglHxTPd68v9nIJGn19Wn1RaojLrNNz71z08KiZtb7kZmZn9hRnifJ/wAgISa1VBu2mmClukPXB2aXDJn9kUZ3K21NmSVGCjuGIzfFEhzmvsjsqkv3aHE1aujpis1Q29NpXbl1FaNH0LjrkmPHs1yoq4ju2naw3VP3LTs6xrzjDialWqzoYEWezaQbp1L1r5MbYBEOI4hKAlsJZHRBowCQI0fqKZryQl9Hp5iTyUpsnWXm+k73aaY9hZv+niH+zlsgaUfUdPE0036i51E1FasLd6cf2VJ/lOK5r2ptWzqoNHSavbvdLyqgYB748yT4mmQ24ZntKWaEE6okdtTKTDs7PUMSBGtLR62e0LOKVCuaxdTY6Y04q4e1ZqRLDcaS7EeaRF1NBmQnIL1PUu20nUFi1CiiotEoFfRxotnqOb6yxS4pHlIYGARAkhKBwCEBpGQzF5D5eoOxDSOngVuCdURGi1RhLCsoV7lfMdGf3UbPShjUj3Fn9nIJIQIL8+RXwI9DE1DqkPvuSF/YF+kYEUvTVPY26tlemNWJshqnSxQQZfZtSVIDbhOFMIseDIqLJdTPmy6O8ZutWtsxs7QJ71dJ+ZVlvHeuolZDeeXId2TYy22fMkgSRxCUBLYQ2OkCaEdoVcAlJKI0LGuSbchriptfTVHsUmiwkNuMwHOaBqlj27WEdV6Kjg0NQO9SYf7KQIEQra92xkpRD07Du9TvTFmfkwfcZcWiDrRkdyomu4jwNJ6kK1av9LSq9/8A8/ZJWaAtxTniIwZEY4/bkQSQQQ4hCA21kNQ+Q+XKHpDSGUYVV/2g/jp2SiJa3MApBkFPmYqXMoGoGOrA7aVrqTklhKvZM13rSv2UgW1RHRQ1F3du2T8LTVpNDOgJqx+BYjBOaep2x8ngpCqyIFVcIwuohhdRGCqlJBUNtANLKRzIGZn2pTgPJ4sw4/qZMdBHYPvG+93MurYcj6ucepWr/TtyH9F1c1Nlo2ygg0mR+fpZa/YiIJSEoDbY6ISgRke9XCSpPpW8WEEkJNPA4M3omme0ZWNmXCU7yNahyHMVTmHEnkSkdRl5s2nOzTTWXhOd6UY+zj+xEEkKqAudNsKlNkuNWwa1Mi7aQLDVJJN3UqjDt7NcCp8pYN5wxzMZPxJLAyZnY/kdomgqIlUWXEchu98SWqI4fscSdJhLrNfSWQ/Cp9Xx7ank07/3nMcwas/ZkEBohFjdQfKHDQuEbYYRhVUojbE5ZEzJd9+vxHrDD0szDjvILMZHIQHeElo8oV7ldNdKw7NNNYjDUDvCFukZ+/wMDiMYFLWOWsyLFYgsSrZtsPPuSFS45SWz07HMO6bMO0kxoONLaPxoSFngVTHWkzFdSTWMFFgvy1OyWX2LiPOr3IK/FGlvQ3qrUMXUce+oXqST+3kEkEEI7eRQwyUCbSRW8ZJFniqJP6I+clwmWRuh53INwdQOuA3Aa8jOzSuLsRWWxqhrhK7KVvpwxqZz2GN8+A/tcAkhCDUcPS9jLDeiVpCtIRSFHBjVDFr82dKZZWsVR3M4x83mkEX01Ib1K4QZv4qx1o8tL9LHeEqofjeFKApe0BBRa2tjesmX0roR8hK1Nrh2TM5uZp5RB1lxlQMuPgI8Co1E1ZxrmncqX/24iCSDaRG/KdLNS0rroNNxMI0OO+6XjHqPZT4WvINYNYWoGYyM7ZFYvmyNUtZY3bLkuGjgwNQuc5nZgcQSQae8vsiIJQKfTcmzHOp063O1s64Hr+c8PmksJuJiQjUMhIj6oQsnIdfYplUTjYU2ptWxGaTj28lgM3jLokwGJhSYjkVXb7EDUZ7R2TkPXayYiVEVMCFLkqlv7xbeRFDdvDmE9RRpCflDzQlRlxXPBW3SJLE2lfjH97jYi5BuvlPBrS9u8GtC27gR8O5xh74f2SBN09ZV/eQIgQSrAQ6I0k0grNwielGsLXkEsG4DcHMGYMwowZjO2QQonMsi+a6lfvBTzls+yDFp+ecZA+xIx2YGBjvLxlsRCg08hTV9qkmkyH3JK9iLI6Lhj07o6akgnFIOPdPtgnYlomXVrZ7WJTsc0WbchMmH0/BQRf4vpfmVtf2HNfc284ybV9KaBmxbxpkRcN3wRLd6Mk5cCxD9SHWnGT+0Sw44GqKyeDOjLl0NfD6yUGfhwZhr4dwEhrRFO0GtN1TQbhR2hx7MDGRqDR8azS8w5Gd3LbkOQ5BCgheB1gbo5jmDWDUOQNQNQMwe2d6Fe05HUiq9j2pkc56foo8Jei9RbyOAMcRwGAkGex7IIcAaNzT2ZBAwQPwEkafp0SFaj1EpSiM1nB0vZzwxoIgmip4Qcehsk5PYHzGKDnRVBTcZ4Kq4qgqpYSFuKbEqIl4KQaD7GpC2gskO936BXsnGhTJZVMI/fwxpTkR0n49qzIrltA/bwokONhM81EomHApky+wqdLWFsIHw/gtFGoa6IEtoQMDHm+INalC+zIzukxyBLHIchyBqHIcgahkZB9hCjVh4vo77olp4SdtOo5SyEg+LPLCJh5XgEQJINAJsKQfYkZBmDCCBkMDG2B9O8hgYCSEGIct6e84pqBoJ5041ZW0iX7og9LdkB+Ap0PaeNQkUsqOFe26ZDqAi0kpCbbkOu26HkpfJbZtq7M4Bnz7CLgVVG9VMUtLTUuQqW/4iWaTKxXhcknBhKhwGPBkchyyERXnQ1p20fDWirhwNfDyxWG/huY/+OGhJ+HUpBWNRMql9jLDkl3T+iWoQIiL7PXZF8g7yBDIyOQyDUMjIyM+CoViWj9J/S2RwsNtMIy4LFXCJ648OOczyI6Oo5KrOiwRclR6hTiJUJTAUQ4DgCRgYB+2yT2PciCiBduARAkgkCDTTLA6nTCYbKn4teh+3cWHDNRyrmJGDupVA9QTFD59MBahfDk+LMC4YMsdrchSQakPoWg0H3tFk1nyOoJqNHubJK2s+fI5bJZccDVJZPBrR9y6GtAWaw18OHTDfw5iJDWg6hAa0rTtBusiMgk8dsF2yIrUtnVWn/kcvZKTUrSmmkU8fyZ8HxDl9Ov7y2yMgj2M9s7Z8FcrEtr9A1CjjYbaXT+QXiuMHIyMhl7prkXRvMQsKfiKQmNc8eKy9ySENch6VWFs4C0DiEkDBgtkmFdmBgEkJQK/S0+cK/S0GCHrGOwJNo+8DUJeoGY4l2cmbtjtJZpCnep3EoyHUJZH7d+dsl40IUoNVM58N6TuXQ1oO3WEfDuUYuICaywFTD9fZMU8COCaSgY+w1jCKZRbaCpCkv4x3Gskh20hsB3VdOyHdd06A98RYqQ78R3jDvxAtFh3WFw6KrVdmxO7tdzPVXfgzuQyD8kRWJLH6BqdGJu2mE/y41EeIXa06aDj3S2xNs/UlkgkUUUpL/wAkYU1f1xRFmQ45HTBoCk92BgJRkNx1OrrdFyXxDqa6nS/ctkJEt6QM4E6zYglOs35p4+7IjPw6QqGG6NDLbe3HZaybTOfOXMHw+ide2+y1AokUhgiMzpYBVlb2PPIjtXmtZc5xyQ87tnu0pC9deF2uLJtM6ScyZ4y2PyM+zsX3aGqy/mNtNl/JjUh/yvdkZHMJdwKazKI9H1C0tGoJ5SVhCOS0QiNExrpH9RxHAcBxBBKQhhTq6rRrzoZYgUrb9ypQdcU6oGokpsb/ACDUaj+y+oTHdcDsR+OkYyI3w6fcQ18OYxBrQFUgN6Npmw1p+sZFmbFVWqMzPujtHIejMFGZ7NWTfRUO3w+h9Cq7sjO2QqQ02Hr6sjiFfV1g9267mempBp2H667Lt+IE049V4fh1Ey926tmeiofIXmR+qEeWRqst9PFiENTf2fEkwT60A31KBOiM7+dElPCc5zMJ/MZR/ZaeI45HTFRpaXPFdUxKtDyHHCkxY7Z+2RJlNRGrC1dnK8aGHHA1R2T4a0hcuhvQVssN/DmSYb+G7Ib+HtagN6Jpmw3pqpaDdbEbHHiXxCl9W0GnYXr7ou34gS+lUd+iofq74u34jyuLAL3FRD9BW9lxbM00N34kMkHPiNKMO68tnA9qy4eDtpNfBrNQMaUYXIvk9vxFl85g+HcTqT+wx8QZfWtfDomJ6ah7fiNMwj7pP1gf2RqtP8Pag/whqb+15iUZBMhRBTvIcxHwZ5LD/wBWkqcXArYNOmw18sjd1hbuH+KLVQj6nEeSzKbjxHJRr08zIE7Tdcwh6ggqEulNgLSptWRU6fm3RNfDyeYa+HBBr4d15BvQtO2G9L1DQbrIbIJBJGN8d95N+YWw+HULnLLt+IMzrWvf8OoeGe3Xcr1F6NPRPXXPb8RpmPB8O4nOZ26mmetvPqNAw/T0nYYu5nr7bwR2jkPRWExY/brKX6u++6IV3uwNVf4+1B/gjU/6BgGXbjxpWaQUgxz5DTrPKZeT1PSOyLMfhOu6itHgdrNUEXM5Aj6jWH5ZSVSFc3RoyB6Kj+wvpfoKjO2iYXpaHsMXUz11r36TiejoexauBWEo5s4fDuH1J/YY1hL9Xfd+g4vQpOyzllBrzM1GRZFXE9DXdl9M9DUeHRcT1V9y4jmOZjkezrxRoz7pvvfd1v8AYGqv8bag/wAEam/QMjPdjfAPwJGnvZlw+avClRoPPIV8M505pBNI8+R8Q5nSrww0p96KwUaP2X8v0NR3wo5y5bSEtt9mqJnoqLbQcP09J2S3yix33Dfe7qqnk3Emvhor4fZr2X0KQaah+vuy7fiHM6Vb4fh1EwjPbrOZ6Og+8rv7A1V/jbaf/wAAam/RtjsLb2BhOyvAQ08rLPk0BB69l3Z2UriHLaEyHdWUzQc15UIDnxGipDvxIdMO/EKyWHtaXLod1BaPBx9x/bRkL1l72/EOZ0q7v0PD9Te/Tt+I0zhDCSNR10UoMHs1k84ikZ07avhrQ9y6Gvh5PWGfhuQb+HdekNaIp2w3pmoaDLDcdHZka2tisbQfDqJzmcxzHIxnbX0zr3Xh0xD9DQdhe4+Is3nK+7IQPZkar/x9tOH/ACQ1KX8DsPbAx2ch9Rjcy7dPucZcxvoy/HoqB6Kk7b/W02JZO6zuXg7f2bwckOujPi+HcLhG7dey/UXPf8OoXGH267l+ovhpaH669LtwMd+RyHIZ21bqEqiLnO2iInpaDs9iFnLOdYeCujHNm4JtPY1+rUsz1134yB9hA+3Iz2J+sP8AtjVf9rbTX+INRpzDMZGRkGCBbGMDAwY+gz3kILvQlajj9GbuhtTikUU1QVRKaQoiJW9bEOfOaQTTfZOlJhw3nDec8umofoKXsWokFZyjnWHfpqH6Gk7FqJKbCV62aPh1Dy/tkhyIcxzHIxyMchnwah1dHqEypLsx4NJN1cZgokPfI1HM9DQ+HQ9OtyYa+R5GRkZE+X6CrMzM/GXbntwMdrRZXF/QNWK30yr+AL9PKB353L3CUhSQaNiGQQPYgkTP6jTbUej5NoChV+n2Z0wkiwsXJy+z4f1/WsO3X0709R2NwpDwa03bPBrQ9w4Gvh1PUGvhuQstF11VXb1EP19kn2Ls1TO9DSd9ZFObPSRJLI5EOQ5jVMz0dCYIaMiek0/5nHEtFP1jVQBb63nWBb6Sh+svlq/Ntnb4guKKo2JBqDVVOeDWk7h0I0LarDfw+kGGtARiEXRtTGWWCT2F7n8QLMmYnkIZ7CIY3IfTY9yEQuUmN+gasP8Ai7aYP2FwnlB8RBKgoxnIwDIYGR9dyFPIJt+fFOJK0npQnCnTUwmps0kJnz1zne3RkD0VJ23+lvn0pr4dV6Q1oenbDWmalkNwIrI447fiHM6Vdv8AD6F1rTt+I03DPfoGH6i47fiJM4xQwyqQ8y0mPG7cGFLSgfTtzga0j2cRS3XHT7fhzE/PnfIyJUdmawjStMgN1FcwEETY5q8cqU1Vw7OwdtJvkIhjciBJCiB7fTvr05mR/wBA1SvM7bTKv4onJ5xj8RAlDIyCB74GNiIF7Clp03a5DyIzM6UbirKwVOd7a2GqfOabJpHmyNezPU3e+g4npqTqDqGOZjkYT7nraZ6q+7/h/F6NX2F9dcS/U3w0bC9Xfmfv2a5uH65t63nPjqL5aVviuoe+RkLQh9vUun10czt0Gkk0H2a1txGdT6lVeSPJgEPqDLZI5A1A9vrvgYBIyOArEfzbP6BqNfKx206rEsg8WUPJ4ueXkM7FuQQQo6dVtMQhuMzYWHq3bqz9Uvu+H1f17Du5EOZDqDqDqGOZjkYztkkCwlnNnbEkzOBGKBXdinCZZlvnKkd9JG9FSjO/Im0TJBy5Q+HMTCMjO6fc9cy/U3u1dYPVkuusWbeD2z4TFrEt6p+nm9nw+eJ2oP7FLalC0uIVG1fajlXjnmyEhR7FuYwCSCaMwpHHdsGkVCcyEfpFyvnY7Uy+E5H6V/psE8Jn2BbkQr4T0+TWVzVZEup3M7uw6CO/R0H0FF1B1DHMxyMZ7sGFuIaDlzXMh3V1M0HNfViBY6+TJh76Yhevu3D/AD9mrpfo9Pd9NXrs7JRlnbIyNTyvRaePbSkX0end8hCiSU+ScyZvpi/VSTfyrQMjIyDF1TN38KRHcivb6QuiqLJ1GfF9RwUOkodBRj0xhw47Amatp4As9fTZQceW+v7AgexEEtGY6OBjZr3MkkJCd21YHUFKXI0/RZ4RLVzk7QVcZTXug/pdI4z9sePAwCSOI4hCRArH7F6npWahm0sfSNz5qYcZ1xTq+6shnPn8Uto7MZErWdVDdc+IcMg78RXg5r61WHtXXDoet58gGtSvB8OIeXjPIyMjIyPiJKMxg9ktrWGqee+GdIXDoa0HZqCPh64Kimi0jXZ+o/iHPJJCMwcmQaSYb7NRSvQ6e7dFaj6ZuEaFdhHxGr6ErWP2ad1k7WJhWUC1Spgx0VAmDMenC3YjQcvaZkOazo2g58QK1Ad+I5EHPiNOUHNd26w5qq4cDltOeCnDX5SSah0XB9OwgSMjhjYgw3lLqC4L9lBtWD6xB13I5DIyCMUKP5chMX04qj99mz4riL5sDUDeHz7cd2ASRwBNhLWRHo58oMaLnuCHoqM0cZEeOmXZtx1SnzcVOluWEhxlxvv+H1f1Zyj9+ywllArlqNavLpKJ6HTue2yq4lshrS1MyGqyvYCcIHUUDPwKdREYt7FdrPGiYXq7508udnxDlcInaSuJ6VvU3cLoLHQUDYwFOxGg9eUzAVrKkji+kV8qd2Eo0mxqCzjF+L7kO6itHwuZIdBnnykhSgmDJWEUs5YTpyYoJ0w6E6YbCNOw0hNNBQEwo6ASSLZyO08UzTrSycaU0sF9W0EHkY3YkESXJRGTi8q5DI5mM9hCnb4RheudOu3IUznUhDULeWQe5bHvgcQlAbZNRwdIz5QiaNgsBqNEgIdtoqA7qEkkV2/ayJk1uqjR7X1s2TXtm1Y3DUcLcU4rtIaRg/L6HsyNfzehW92AltSzaqZz4Z0jcOhrQdmsM/DpxQsGERZpBCOlC+xaa5jWepPXr2+HcXhGP3GduKghpXPXMv1N93QZr1dJe1tcuB3UNm+FynnBnP2JNrUG6+S4CpZoMsGEoUs4unJDoY0/DaCIkdscSLy6gh82dm3cB13kOQyORkMmfhbTzXDRwQNUucYnZpp3McWzXUiq2wMD6bcRgYBECQKfSsieINXEq0SLiOyJN2+sSr5hAf1BIcDjz0lddIRp+DYWbk46NDddHtr92arvq4Z2E9WG0diP1a0kKsLtmjspAa0bcOhvQM9Qa+HySDWhq1Ab0nStBqprWAjDQ6zg5Gez0ooNcszUYZd9RXefGQrpxWtSazOaW9BaVdVROaxo2g78QK9sO/EdQd+IVksOa1uViQ+5Ke+2aZW+5H002SE6fhEE1EJAKIwkEgiClcCt7k5W1fXuz3IVcxBR9hYESoORkZGfLXo6ktgsIGqncv8AZpl3Dwkp5NuFxXtkZCQYIEQabNaqHS6YwmWbUUTrFbwlagIg/KekntAJEVEqS5KXEb6r0+wOQfg+Htf1Zqz5L7foOSj8euZXpKLbQ1uiZXLQaD8ODMdJZjoLHpVCRIgwSsNfRIxWl3Mt3PtY0R6YtGnJignTDxhOl0BOnIpBFJBQNQtsMOdkeO5KdrKxuvR2ZJJXNv6oxBhLnSI0ZuKz9jbOdKu8REDLsIhSNcpKPZIvXurY9lM70pxe5OFlNo10pvYWyQlORpzT5VyLG3E+1bhFKmOy17lgOOm6oEsyT3kRmOngaUg/LaL7Fv8AMv4iS+pZbRJTsKRTayh2TfQSsvTuEPTuD0yh6ciDj0JgOX9IyHdbUjIc+IkNId+Izwd+IFqsOawuXQ/cz5ANZqPx4CWHFhFXMWE0E1QRpmQYTpcHpdIe05JQHWlsr30yx+Qu25e61jvDhuTXYEBqA12KUSE3Fyco9qKH6aJ4PoFSGkBdpDQFX0EhEmNTWu3Uz+Ge8hjbPYQIULX8Ig4rg3Jc6r/ZHX0no6+bQ1A1h4+5A0nSi0sci0s/Sg1Go/J9R0zIiZJINZEKeEdpZuflT5fqOmowUZZhzoMh3UdLDF3P+Z2vZFs5cII1jcoCtZ3Kid1HavhyY+6M7H422HHjRRzlhOm5RhOmFhGmWSCdPQiCamEgJjsoH075te1Pakx1RXtqZroV3Y6smmlqNatoFe7Pdhw2oTPYtZITb3BzFbQWPUyy9i7Jk1qC07qWSpSr6coLtJbgU+4sZ2IVbHpoPbfP9ax8Bn2FsW1S1044uHuhXn3Ur3Vhi9Z5xT7MAkChqDtJlnOKI1Y2XpUGZq8mCCEczYrFCQpDIMzUY+HldydX7q78DgowTKjBskgP21VED+t6dkSPiPgP69tXhI1DZygtxTh+cm1KCa+U4EUU5YmwXILm8KufnKiafjRwlJNl59TtEToZbN11CCQjsvX+jXbVtY5YLjx24zXYtaW0W1uqcrfTbPOZ26je5zO6G0T0k58RsLvICArUkQgvU6ArU7xhWopig44bq/CRAy3IgRCM31X4qcJGqn8M92mZHsJbfUaUjieBgYBEI7Knlw47NFW2c7pl6ZbgccI/E3HdcDNG+6GqFhAVJhQik2i3AfvtkabhfLqPfBmOkodEPy4UQPavpIwkfESKgSPiHYOCRq23kB6bIkDP2VJVNS2kU8JARDjtjiRb2z/XsNqmnOYG20tI+x1Or+GKFrq2Pbqh33FXULnKaZQy32OOJZRbWyp6uzTrHThds903pn2ZJBkCIGWNjPsIU7XOS0WEjUUjrT+6ke6U0jyS/crNjozMDA4jA01VelasZ5y3ZZswzkSHJLnhY6rq+mzWk7duqD0p1/toYHzK3NOU9MhIsq+GJGt6eOJHxGISdeWzwkXU+WDPPiSy4sIqpjgRp+aoI0w+YRpdARpyIkIpYKB8shhylhOCXpr2daWyvYhVsengdkl0mI6jNShU1pz30pJCfstSvc5g0wzhPbevdaxqKY5ZpSSE9jrqGG7W1XPX2EIbXQi9sqiiyXFaYbC9LuBenJZBVJOQFwJLYNBp8xKBmEmFH2Z2IUbP8EvYOL4Nynes/wBzK+k7Ec6rIvGRgGQJI09S+rXZyTdOxsUQCWtTivDChOzXHOhTR3HFOLyDPtobsqN6Vru1eEm3nTNs9uAiK84EU81YRp2YoI0u4YRpdognTsIgiohICYrKB9PFcV5TY+0Nk5Moix26he6VeElk62IUKJ2ngg5LYbCrmCgK1FDSF6oaIL1Q4YXqKYofPJyjaNSm+zOBNf8AUShTsdCv7FqJCK6oOU4RY7Xn0R2rS0XYOdtWz153kNJKCojCwunhLB6fhGHdMoMS6qTD78jPYZ9yE8jgM9JAvpPp6/waekdWKLFnrR+IwKaoVYLmSSjN2VsTAUeT8NfUOTRKls1TK3lOr8+noDa2Usto+0nt9GaNNsc5nbqd/L4o4/XsO2dMTAjyp70xfdWR/UTu20f6EEMNm88kuJdiiJSe1+QiM1Z2blg53ae/2X2V7VJY+wIhVsdWWynCRqmVyd8GnZPTk/UOJyUpnpP1tOqcHnm4DNpaqyfhjxXpS4NAhkTrlLRLUa1edJGo4bHpY32lornYDTjPCF22r/qJ40yxhvt1Q9lzv0w1l41pSFzozYVdQUhWo4aQrU7Isrr17IoGOrYeJ+Q3Gas7Jdg73xJBxZLLyH2/sZTRPR/EkHuQSQpI+EF7Az4psn/UTPBFd6D8dzqNA4LcqdPlFDYs7NSkH4I0B+YcTTzTYds4kJM20fljIz9hRxvUTvKb7SA28h0u19wmWVq5rL3OGz6eL2S3ehGP3MVDHp6/tuH+vYd6XnEEajPv0yzhrwrVwTZWTk97wwLN6AqPqGI6SJ0ZwEsled1fBnvSOIUEg90hH5jgM9JsXUn00A/fxafk9aKJGUCZNU4F99fVOTQxURY4kXbDJSrF+UOQyM/Y6cj9OJ2qWltK7eEgL1DCSF6paIK1Q6YXqKaoLt5rgVJdWDGm+Xru3UL/AEq8VbHqJ3bqJ7pQBFZORJIsdst708YzyfnqGehX+HIvazoq8hKMgmZIbCLqagJ1HMSEaocII1OyYTqCEoJt4SwiSy4M91090K7vIGexA9yFQx1ZTKcJGqJfN3xafldGVnIcLkmxY6ElQPtraQ1iXZMxCky3ZSsgz8GfGhBuLjskwz26jkdKH4NNx+Mft1NI5yRpljLnbqZ/lJGnGOpM7dRvcIPnjNdd8iJJZGRkZ71oS4izgKgSPtNOLcXG7dTveT6jgYJsJbFPH6bJewdWTTc2QcmT4mXDachPk/HF2xyaMge6G1Org1bcFM63U8Mgwf2lBG607u1DI60/vIQWPTQ+2wf9RMFGx0K7tsnuvOGnWenD7dSv85nn0+z1J/a5IQ0vunQkTo77Ko7vaxCkSSTRzlAtOyzCdMuhGmCCdNxiFxFZhv8AfTs9Cv7bp/r2PbgcQTY6Y44GAQJIwGG+o5Gb4JGopfQh+TTUvkgSGycQ8ybLiiBkGI7klxhqPUMTJzkxRmM7Y3JIxtgY8un43RhdrrhNNPOG6531jPqJxdti/wCnghls3nUIJtHZOe9PEMwXucRnoRhkZHIchNf9TK8WDCYzywmpmLDiFNLGm2cMduo38y6ew9cz3XVb6xrtpmujX9tjI9RN7oFe5OdSkkJ7HXCaaUo1q2wMDGxAi9geyQRjIqGOS0FghqCX6mb5K2T6WW2rmnAuo/FRhiIqQrmzWsSJLkhzkDUE+4+hZB7EPqFI9tj3PwMNG+602TTfbqKR0YPeRZFHVnFLt1NI4sjT7HVn7ZGRkakkcIYqWPUT+2ze6EDvaQbriNPxCCaaEgJgxkAkJTs650mVqNahWs+ng9tk7150SUuG9Fkols917W9JW7KOq4lPBPYouSPw2/lOmTCdNNEE6eikE0UIgVVCSChRkhJEku28f6UDvIcy48wpQyMglBBGtUCOTTYs5RQ4a1GtXloZnXiicwT7LcU1KW8iKh5anlKTtgF7BSuxBe30Pl7H2ZB+DTsfqS+7UkjqzRgzCIUh0N0U5YRpl8w3pdognT8FIYgRo59v0FtL9ZMGm2eEbt1G91Jo02z79upX8N99Iz1J5dt6/wBKvENrryfp2yXejHM/cU9j6J7Oe5aScRZwFQH9qJnqWH2d5L68rfIz2Z7CBEKiP1Hmy4pGp5nN3zUcv08tB5JRZKeRR1LUa1AwfsPqOIUWN0t5CUBacdnH8vioo3Qg9y6CM46inhNhDLTfkvrLoI2gMenidmcCY96iSKZnowO28e6s/v06xhvt1I7+cafa5y+2/e6cHehseXfNhomsPsLjOjTn+T9lb2RQ2vB9Rw2IYGAlORWxegyJL6Y0eS+qQ95kng6mYUqMLFjrsqyDBhRgj2WCCSyG8DiHUDgMYCGzMKRglFjcu6Mych9CSQnbIyMjPmtLZEBDjinVivZ68ztsnuhBDSOq4lPBPYZ4DqzddGAmM8sJqpiwmimKCdOPGGdOoSptCW0dts915w081wi9uo3cu7pUaFVk8pzHdc1/q2hRPdKdj7GytW4SXXVvr8CCH/gyBEMAkirj9V9pOCGp53t9hQzOi+R5JaeRWUXpPKQFkDLbIM9mwSsBC8h5YL3HDJpwRfUPIIYHEF3adj8pH2S1pbTYagIgpRrVtpxnL3bqN7jHFGz1Z3cdTDNaYMVAS2hHjspfo4h+5iA10YnbbO9Wf2Q5a4b7DqZDXdeVvSUg+J1dmia35n7SLGEy/deB+/cggogeyCGdiIJQOArYvRaISHijsy5BypH2CFGhVXLKVHE+L12VBaQZAiHSyS2MDgEljZpQfBKwErHLI54B/mSfsYPbHZTseng+dbzbQduoTQkalEma/LPspGelB7b97qTRpxPt9ktRNptJ/rnxCa68rtdV021q5q7aWx9M7tzSQOQykKnxkg7SGQdtYC0PpbS6lZpONqB5oN6hiqHzyCDv4RA9RRSB6laB6mB6lcB6kkA9QyzHz+aYO7mmDtpig5Idd7yLIwCHHINsE2C/KFGMhASK5jrvNIwQ1NPwX2VBM6MhJ5JXuVnH6TxkFpBBAd+n/kF7gvYH7jgCLAQYP9WfyrP3LuiMeplEWPG4820HLqE0HdStkHdRSVh2ylvAzz3tpNa2kE0jsM+Iku9d8adV+T7FxxLSbW2OWe2n2ecrc3EJCpsdIs7Ng4ff62QDdcUM/c42I8DIR7hCA4rAawHSBowCLZJGpVfF6DRCVITGYlyFSn/skqNB1M0pccTI5PtufkNZgvqgO/TYu1lHIen9+h7PM4PgDLt06xlzuNaUB22hsh3UrKQ7qOQoO2kt4Go1eSlZ6s7ttXuhB2p5fpZfnNRJKVdxmBMsHpqt4ti9CSq5mqB2EpYN9xQz+yJIJRkLTgEQUQQeAlf5FqyaF4HP3PCkEQJAqYvNaE4IalsOZ/aUk300lCuSTLIuovE1DOAT2Ap7I5jOQRjPZF25CQedlDhkGgcR9BVx/Twu61k+om/YaeZw326jew3vV3JEklEou0zIgqUwgKtIaQq9hkFaijkFalB6jeMLvpiw7Jdf/bkqBKCvfZQIJyZKSZbJSagywah6XATHMzhxyZbFlLKHGdcN1f2n0FHP9SyJDJOtyY5x3j7cjmOoOY5iOsiHUIwag7soED2gsepll3Tn/TRDP7GtZ6ELtvHupP7GZb0cN38pI/Eb4XqCUYVcTVBU6SsGtR+HG2AmM8sJq5qwigs1gtLXCgWkLkwWi7kwWh7kx+BbgfgS4H4FuB+B7kfgq5IHo65IK0rcJB6dtEg6awSFV8pAUw4kfTbH26EmYNsxgcQ3HyGWCISY5BLAaZIMIILSRCvZ5KSWBnA1BYepkfbVsw4khhwnEC5hdVtRA/DzMgTxkPUGOrkdUcsgjB7UDHfqN/Df2DBJU8q6hoCtRMEFakWF6glKCreYsLlPLH18iGlOBmjsZAa0bcuhr4fWawj4cPBHw6jBv4fViQ3ommbCdL1CA3TQGgmKygEgi2wMDAx2429h7b5BpSYVGYWHKevdC9L06w5omlWHfh/WqDvw6bMO/DqakPaHuGSf0/Zxw4y40MeIge2A23kIawFp9jTgIR7tJIiWZJC3sjqhp0JewEZlLZb4JF5YejjmeT+30/Yck/ULTyKzh+leMgfm5DkOQgseni91xI9RO+9ZgSpIj6PuJAj/AA8mrDPw6jJDGh6doM0FZHCGEN7cS7sjIyFPtoDlrBaC9T1CAvWlOgK19UpC/iHBIL+IzIX8RlhXxEmBXxAsjCteW5g9c3Jg9Z3Jg9W3Jg9U3Bj8TW4/EluPxJbj8SW4/EtuPxRcAtV3JAtYXJAtbXRAtdXIRr20II+Ic9I/+QXHA7qaqkhcjTj4XEpnA5WNB1hTJ9hAkhQSWRxIg0eBzDjuB1chCzDbgec9jWY9w3kIJShAikyjAedSw3ZTTnSPuGHlMO10tMlkWEQpLLrZtrMH56uP6mZ3TH/Txj9z+4jVc2YI2hrZ8RfhyQj6FqWRHpa+KEp4jAxvkZGQuWy2HdRVbAc1rTthzX8BId+Igd+IE5Qc1tcLDmpbd0OWMx0KWpQwX7EZb8wZglYHMNgg77jgYSjAT7A/cE3kJjkCaIVsXktJY21HZcj+6pZ/pnm180mLqBkKIH59OscW+7UT/Fj7WJR2E8Rfh9PcET4f17QiUVdCBJIuzIyHZ0dgP6sqI4e17XJDvxCMPa7s3A9qi4eNydLeB+4wMb4/ZiB7ltxBJDaATYNAwXY39SIMNG84y0TaRbWBQY7izcX90XsKKy6yCPIcTyKzheleMH5SLJw2PTRu67kdaf8AYxoUiYuHoS0kiF8PoTQiUVfBGN8h+fGjE/q+ojh/4gw0h/4gy1B7V9w+HbGZJBlkYHEcRwHAcBwHAcBwHEcRxGBj9lwMBJA92dln2owQQrmIMXppDzhNItJ5zpH3kZ9UZ2vlpktfUTIqZLL7KmHMAy8lOx153dMuGIoWo1r80OrmWKoPw+luiDouqiBphthPttIsokQpGtqlgSfiIgSNdWrwkXVjLH12wMAkgkjgCbHTHTBNDpDpjpDog2gbY6Y4DgDSDSOIwMffkgcAot0BW7RkQ6pEFukOqOqOoOY5GKmEaQksF9BqG05q++p7A4rrLhLSYt6/roNIMhxB7Y8FAxwj9kuwYhFNuXpXmiwJM5UD4fzHhA0dVwQhCWycdQyUrVlREEn4iR0iVryzeEm7sJgP37MDAwCSCbHAcQ2ypYap5roRpmeoN6TeCNKIIFpeMC0zBIFp2AQ+QQB+H4Bg9MwVBWlYphekUmHNIvh3S9gkP1E1gKbNI4DiMDAwDL7ognZe6V4CljkMjmY5GM7kMCpg9dbTfAhd2ZQ2VKNR/f0VmEq5EpPIW8DpKBg9iP2PbHYkjUqOyTDGzryGEzr9SgpRrPxkRmddo2zsBXaGrogzFr2ZesqiKJXxFMS9YW8sOyHZB92BgcQSQhs1nGoJ8gNaTcDena5sNV8BkJVxHLILy+4cbQ6HqOufD+kIyw/pGc2JMCTFHEYBkMfcZBKHMKVnyJEOKcpyLHJpAmy0RGZkpct79gbWbaqiwKS2HmicTYQjiOGDB7Z2z2UsbrTdp121HEiU7KX44kKRPcgaCWRN2WmtPCX8QzEvV1tLDrq3leHAwItfJliPpN4wzS1cYNuoZBvuKH1BEYcmRmAvUMBsK1S0QXqp8K1LYKCruwWDsZZg5TygT7qFVOoydGcBWFlbV8iCCs5iAm+sUhGp56AjVyiDeq4ag1dQHwSiXsa1YkVFfLErSBGJdNMhDAwDL7TPafjIgyyp1ddCKO2QcWTaLmyOa9+xQ5aor0GYmS2JUZMhuVFVFd4hSfBQsdOJJmMw0T7h2Z5I0JySGipoId1jMS3IlyJavFgEQYiOylxdKPGI9bWwgcpwyzkfQOWkNkO6kQQXfTnA5Kee7sjIyDGRS3fAcsHzyLmr9GsZ7EOLaDOoLBgM6sEe7gyR+oiWpAl1MCcJekn0iRHcjrwD/YSIJTkVVd0UkWB9BqC25fstVYHFcYeJ5GMiwglJbcbNpSgZDAPsQg3XJdu3CaddW8v7qDSzJ4i6ehRQmR00H77ZCyUsOVTTx/ImAdAFUcpIfYdjjOd8jIyMjIzvRWXqm+QWSXUWMM4Egz3yM9keW/FDOqHkCJdQ5YyHUokom6WaWJcJ+GvAx9oXiIgRCorjCE8SF7bemQozUf7LTWZoU2slpMsi0r+uSk4BlsfZ9Pu4Gn5cwRKuBADj63dlLSgKsIyR82hj5rDBWcMwiQy52czw9VxZAlVT0UZGRyHIchkZGRyDL6mHI8pMtjkLKJ6+MeSMZGRnsgUypKW4EVgINBAnkglEY+oURLRO0s06JUV6I6ZfeEQIhVV3VU2jgQtbNEFl55T7n7MlRpOmteZEfIKTyFpWGsGQMgYMtj+6r6STOEOth14W4pzaRZMRw/avvAyIxgh7bGezcl1kNXshAj3MZ/f6CdUtyg62thzIyMjIyMjIyKGd0ZGdtQROm9kZGRnbIp4pS5Wc7SJKIqHr5xQVazDDV5NaOJqJp4EslE8lqU3Y6ZW2Rlj7rAJIrK45K2WiQkWE5EJmZLXMe/aGnFNLqbMnkl7haeRWtcZGYMH91DgvznIVJFghbinNpNsyyJE9+TvkchkZGRkZ3h2L0MRJzUxGdp0BE9t5pTDmRkZGRkZGQSsCDM9bG5CSwmWw4g21ntkZGRp3/H2nzDmyM9kae/COFesyQSuJ2FZGtBYVcitWf2GOwi3IgRCvgHKWwwTSRLlIjNWM9c579qjvqYXV2SX0fULRzFpWdMGQMge2AZDH2ZFkV+njMk8W2xLtGYwkTnpW+RyHIchyHIZGRyGRkZCXlNLrLAp7W1zA9Uz4NPSOL+2oI/Tf7dNnlkST4xs+2RkchyGdoVq9EEWczMTklt2unzYLzkYUCBhOxECSIMBUpcaMllAkSEx27SzVOd/bIspUZddYpfSQWjmVnWG0ZkOIMtjGd8b48kOC9Pcg1zFaWTMSJbUVMu2dk7ZGQahkZGRkZGRntztFkKiPMvJkNAhOo31S06ekmEaaCdORiBafgkCo4BD5NXj5RADdbDaXtJioltHp6IYVpxoK00sL09MSFVE1AojXHmBwuSDyk9sjIyMjIS4aFQLwlBKjSLSjRNDiFNr8GPCWxEIMJUtceMllAfeSyi2tVTV/t0SWqMuvsEvpDjZLFlVm1sex/ZVtOuYEcGGs4Eu5JIWtTqsg1DI5DIyM+EiNYaqZjoa048oI07HSI0ZuK39tNoDdecp5jQWhbZ7ZGRyHIZEC2chCPJblN2Nc1ZokxnIr3gx2YGAlI4AkgkCFXqlKjRksIDzyWUW1uqYv9wiS1RV19gl9Je4WjkLOqNJgwffnw4FbTEkGrkJEpuKiZYOS9uQMxkZGfARGo2aeY+GdOEGqeG0EoSjxfQdVtIOXHIHYRCHzOGPmkIN2EZ5exqJJdRJ757lpJZP0kN4P6ddSH4r0Y9sglbRJbsRyDYtT0TYbVg1LiOw3vDxBAti2IQK9UlTDBNJD76WEW9uqYv8AcocxUVcCwS+j6haOQs6nkDIGQMhjypSa1VtWmGX1E+zREDjy315BqGfChCnFMUMp0MUEZsNMtsF3LkNNB26hNhtwnUbTbGYmUcp9YM89mdo7psPlgyFk16iEQS4tAKxloCLucgN6kkpEXUKXns753PCilUcV8SaSVHH02yMhtxTS621TNKXFbmsyorkR7uztnsLaurFPm00TZCRIRHRa2ypq/wB0iS1RV11il5JHkKTyFlVdYLbNBmQwMeNCFOKrq9MFIsLfiM5GRnwx6mVIEagYbDTSGU9i3UNE7dwmg7qUOX0xwOTJD29WfKv2syxYeCokdev2nMeml9mRWyfVxBfJdaSzezGgzqRswzZxJHZLr48wS6J9kGWDGQSjSdZbFKEuK3NZkR3IzviIgRCuqTM0NkghKlojN2doucv92jSlxlVtol5KT5BSciwrUySeYWw4aRgGXhSg1qr4BQUZIisbU3t897ba3VRqJxQjQY8Xtfs4sYPakIPXMx4KUaz7qn/XbWKuU/wabe99tRt8ZXbQzOhJyHmkvtSGFR3do86RFEfUZiPYxpW8yCxMKZUPxeyqsvUlPhJnNLQba+/AJIbQa1VtUTYSkk7TbBuG3YWLk5f7wy8phdZbk4lCyWX1EyA3JRMhORFGQMhjvIsiur/RpMySVlZHKPwNMOPqiUYaZbYTsQfs4sYP6iUJE6RJ8cBPTh7PL5veCne6NhtqJrnD7SPB10v1sUahh5T2xbeVGEW7jyB9dptS1KEiK7FWEqNJ1ll6xFrA9SjHbgYBEI8ZySuBWIjpIsbWVs1CRLmOTHP3ptxTaqu3yGX0ukHmEuom1So44g0gy7qqD0iMySVlYnKPvQ2p1UWlDbaGk7OPIZTIvm0CRYyZPlaR1XElxIS3ejF8KVcVNr6iBYNdaD3U030skOJS4iZGVEkd0WwfiCLdMyNnGUPIsKlUbZpxTS4UxMtm3hcFb4BECIQq1yUIsNEdANRJFreJYDzy31/vmcCutzQcaYl4gpBLE+pJYcbU2rAxtjasgk6Ys7Hrn3xKpboZYbjp2flMxhJvFqDjinVeakZ6k7a/e4QfFUO9SuH1D6Ok/wB1PO9VGF1C9Sx4Idk9EESwZmAxOqUug0mk40lUV1txuWxLinFewMDASk1HAp8htomyDjyWk217yBmaj/f4ViqOcKyS6SVEoGWRMr25CZcFyKeBgcREinKdSRJTa2Ge9iK5IVFgtxgR7PyWoxSLpxYMzUf2FAxwj7X7/Ul+LTzuWc7XCOnY90KUcOQhwnEZFtD9M/4CUaThXIIyUU2E3LJ1pTK6yZ6Z2XGKUyaTSeBGiOSjhVjbBEkk7S57cVFjbuSz/wCCjyVxzrrclhp5LhBxollNqMA0GRk2ajjsFGas53p0d0SsNYJJJSFuoaTJuFGFKNR/YtoNxbLZMNBxwmkPOm874qBzErbUKMSe+knYGRKYTLYcQbS/DCnuRDbeQ+iTGRKQ6wphdTL6rVilKnYVQpYajpaIGokiyum4pSpjktf/AAiVGg4Fyps401LpZyDLkJcBuQluP6B1+Qllhw1OL7EoU4qJXpY3lWSGg46t5X2dHG5PDIvpPBjx1a+E/bUKMs95KNJwpfq2Mi2h9RHiYfXHXGkJkomNNuswoch12FWIYCUEnZ6SllNlfmoLWa1f8PFnLjCDcJcJt4nCC2iUJ1SThPsOR1YGNmIy5K48VEVAeeQwiVOckfakXI4jBRY+RkWEn1crxsr6b2drpPKv8EGV6V7lkZFhF9M94SEXq9VmrU8pqOlsgaiSJ9u1GTNsnJav+KQ4ptUC44iNPS4CVyBlkPRUuplUikhbZoVHiKkrZaSygS5yI4dcU8v7WmjcnMgjFtK6Ebysucmcif8AnheGql5LIkMlJaWg219uBgEkzEOkddEaC3HQRY2floZKwvzUHHFOq/42NOcjiFbcgxKQ6QNJGJUBqQn0rsQkPIWJljkGQP7Vts3VstpYaGcCbJOVI8tevlByFfnR4UmaTjv+oayLKP1E9mARCLUPSBDq2o5JQSdnH0tFPu0MlLsHZR/8gR4EW1W2cS2SskPJc2NBKEqtbkCTSutBaDSeBj7Srj8EZGRbS+m15qpX8nkJP8zpcXPDCkdB3IyJjHRcGARZEWnefEOpajhLZJ2U4SCl2yGkzLhbpmZqP/k23FNKh3HERrMjJLqV7GgjEmvakFJolpDzK2VGkGX2MON6h3I5BbpNIdcU855qlX8vkEr3mlxl+KC/zQHmydbQhSlxaRx0RqtqOEtknZSySJNohspt0pwOOrdV/wAuzIcYOLccRHsyNKHkr2NJGHYyXUyqFBiRXSI4wDIY8pFkRmvTtbWEnqK89Uf5cjIsf83wYGAjLa0dV8MU6nQxAbZJKCTsp0kh+zSgpl1yD0hx8/8Am2pDjJxbjica2JSWpSHBnOxtkYlVbD4kULqQ7GdY2MvHBZwM7S5HRa+wrD987WP+T3YGAhpbpx6V9wRaVhoJjEkEWNnH0NlJt0pKVdGsOyHHz/59Dim1MWq0HFuCWGrBCwThHsaSMLYJYkUjDoeonkB2K6yMDAx3R2OsvZSyQl1w3V/YV54d2sP7u2BjZthx4MUchwR6JhAbiJQRNJSPoDURB+c0yJd1wEi2U4a3VOK/6Rqe62I9yRBizyTcxDgJRHsaSMKYIw/VMOh6gIO1EpsONLa2wMAkGo20E0gfUS3uavsYX90GJvu4ENqWG6qU6GtPmYYpY7YRGJIJsi2yRBcltBP26ElJvMh2wdcGf+pQ6ptSLJwR7oiDNoRk3MQ4CWR7YBtkYXGSoO1DDgdoGwujeSG4D8cKVwHUSYfd4pGN8d2BgYGBgY3iEZLJh9YKtlLCaLmbNKwgIiJQCZSQJJFsayIOS22w7bpIpN4HbNawt1bh/wDXIcU2bdm6kM3ZBm4JQRaNGEyEqHIj34kDbIwccjCoCFBdNHWFUDAVp9AVQKB0T4OlkkPlEofKpY+Vyx8slj5VLHyiUCp5IKjkGE0LgTQEE0LITSRyDda0gJjEQ6REOBb8iIKfSkOWjKA/dEkP3ZmHLJxRrdW4f/aolvNhFmpIausBq6yEWyQizZUEyUKBOEY5EM78SHBI6SR0UjoJHQSOgkdFI6SR0kjppHEhjbI5EOqkgchJBdky2F3TRB2+Dl4pQXZLWFS3VDP/AHqXFIHq3Qmeog3cKSE3pkEXoTeJBXLYK4YMFaMGCsGTHrWh6tsx6lI9QkeqSPVoIeuaIHYskDuGCB3jBBV+0FahIL1Aswu5eUFz3Fg5CzBrNX/4SSjIdZwE+sh11jrqHqFD1Kx6lY9QseoWOuodVQ6ihyP/APoT/8QAPBEAAQMCBAQEBAQGAgEFAQAAAQACAwQRBRIhMRATIEEiMDJRFEBCYSNQgZEzQ1JxobEVwTQkU2DR8GL/2gAIAQMBAT8B+fAJ2TKGd+zUzB5zvom4J/U9DBoRuShhVMOyGHUw+lCgph9C+Bp/6EaCm/oRw2mP0o4RTH3TsFjOzk/BH/S4J+FVLe10+nkj9Tfz2Kgnl9LVFgv/ALjlHhtMz6bprGt9Asj90+phZ6nhOxSmb3TsahGzSjjfsxHG39mhf81L7Bf81N7BDG5P6Qhjh7sQxpndqGL053umV9O/Zya9rvSVqpKWCT1MUuDwu9BspcInZ6dU+NzDZwt+bQUU0/oChwZo1md+yip4ov4bUT3Klr6ePdykxr/22qTFKh/1WT5nv9R80PI2UdfPHs5R408esXUeK0799Ex7X6sN05ocLPF1NhUEno0U+GTxajUfZEW/MAL7KnwuWXV2gUFBBD2ueE1dBF3upcXkOkYspZ5JPWflWyOZq0qHFp49HaqHFIJNHaIG+rSpqWKf1hVGEPbrFqnNLTY/ltNhcsur9AoKSKD0DVPcGi7ipsUY3SMXUtVLN6isq5RRiRYiPloamSI3YVT4wDpMFHIyQXYbqanjmFpAqnCns8UWoRFt/wAppqGWo22VPQxU+o1Ke4NGZ5sp8VA0hUkz5Td5ug26bFYXKbG9wuxun3UVHJI0OzW/RHDpP6v8KaJ0f8QfsnR31CLfk8yzK/COZ8Zu0qmxe/hm/dMe14zMKqaOKo9W/uqmhkp99vyZjHPOVoVLhbWeKbU+yGgVTibItI9SpqiSY3eUEyO6PhOUbqnigL7PdcrEXNZAQDuhWwNGWPxf2XJqKn1+Bv8AlCgiYNlUUA3j0KkZ77pw+QLrIu64KmSE3YVS4lHNo/QojSxVVhYd4of2TmFhs78jpKGSoOmgUFNHALMU9THAPGqmukm02HAJjLp3gbos2tmKjoWBl5tSVWxUkbW5fdNxGmZo1f8ALQ/dNxanO5Qlil9Drqvg05reylaiPOJ6QUD0AqkxB0TbO1CimjmbmjKqaWOoFn7qqo3058W35DR4Z/Mm/ZDQWGyq8SDfBDv7p7y43J4tCjYSQ0d1VSZnkdlSB2a7QhC9/wDFf+yqIoYwC0d02eIekhNkBXgduE/DoJNhY/ZPinpt/G3/ACj6bJw80myJv1N6WnwqOd8RzMKpMRjqPC/RyewOGV40Vbhxh8cerfnmMLzlaqPD2weKTVyfI1gzvOiq6903hboETxCiajJymOk77D/vhROsSn1BboN1ND4C9xuU2OJ7R4VZ9P4o9R7KCoEgzNUUifqFWRhsjgn8CigdfJeejZON+tuyKvbVUGK2/Dn2912uFXYd/Mh/b5yKF0zsrFSUjaYfdVFQyBt3KoqXzOu5E8Qgom3sB3Uri53CJ+R11EcxL045mkKB/wCGEHJzS08yPdU1QJBcKM3asRd+KU4olFFZfIJ6AE53Q3oGyPC6ocRdB4X6tTXNcM7Doq2gEv4kW620PzMEDpnZWKmpm07crd1VVbacfdSyukdmcr9ATQozk19teEdPJILsF05pabOUT7aLOonWJag9B6sWHmsTJ2Np+YppTI4uKJ8sv9uhoueB0PQOgI9FDXOp3fZMc2RudmyrqHnDmR+r/aItofl4YXTOyMVPTNp25WqsrBTiw9SfIXm7ukIKNt1McsJPv/1wp/DT/qpYxKPunRObqUHou7oOUYLlTQBjc8myqqhpJZF6USifKedOlptwLboBZVYdRPTR1zqd32THtkbnZsq6i534jPV/v5aON0jsrVS0zadmUbqsqxTjT1J7y43PUE0IDTTcrEPC4RDtwi/8Yf3KcU2UtWWnk75U2jadnA/qoqZsbsshFv7o1tLT+jxFVNfLUerZX8wjgBxCaLcDorq/U7dHovqqGsNO77IEOGdmyxGizfjR/r8o1pcbBUVIKZuvqKqqkU7bnfspJC92Z3kMVP8AxQfb/tVrs07uFP8A+If7pyPmuNkHe6ab8SLrloaI6HgzgULFFDqduiUOPdArD6zlHK7Yo6ahYhR8v8Vmx+Tw6j5Q5r91NM2Fud6nndM/O7yAmKPSx+/+lV2MziOEbctGPuU5HzXC44D7dBNk03KeEE0cHbcBqo6SV/pahhsn1EBfANG718JD7lfDQ+5Xw0PuU6kjP1f4TqI/S4FOgkZuOPfg0rDqv+U9Ob9LlWUpp327fI4bR8w81+wRcB4nKtqzO+/ZX8gJhReWsJH/AOvwGqqRkayP2CeEQj5RdZE3QBsnCyjPbocLhM+yy3TRbgNVFSyTnKwKmwEbzFOp6Wn9Nh/lSVtN9TiV8fGT4GfunYmQdGBHE5D2H7L4159v2T6gkXXxQ7hcxp2V0Wh3qToSDouW6+yFK1sWd51WyjflKo6nnNyHdTwiZpjcpI3RuLHefS05nkDAmtDRkbssTq7nks27pxQKv5AKpGia8R7p7SxxaUDY3Qr+a/8AF2/0po7bpwRRHCyyq3S834HQJz8ybvxui4Jpss6zK91h+Gc38STZS4hBRjIxS4nJNubBOcTvwGyJ4Dg/fgDZCT3XZMNwiirKnldGQ4JkgnZzGrEabms5rdx5wF9AqKm+Hjsdzuq+q5Eem54PNkCgeq/AKB5a64WJRaib340UvNHIf+ilbZEJrC7ZZI4h+Jv9l8Rb0CyE8l/UnsuMwRHRkCsE66I4RiyOmqJv00cPNlDVW12UGGPSyOu67LccOyHAIKTdbrumsLlfRR7Io7oIGyoKnlO12KIynRYhTcmS7dj5uFUuY852wT3hgL3bBT1BnkL3K6tdZQjoVmV1dA9ATCqg5qVpO2yItpwa4tNwpiJA2Ud1lUrvh2gN9R/wt+MbnNd4U5tkR0lmZcsLlhE5NkXE9WHSZJwuXznODzZykicx1kNk3REcArcAnNJ2TWWNijlGyLtLcGGyJunboIlRy2KopudHk7hTRCaMxlPaWHKfLhiMrwxvdMjEbAxvZYrU3/Bb+qKJTHK6cLlcpOGVAoFXV+LVS/jUz4uijvJE6MbjVA5XgSaKV5e8uPSw5nAOUoy+AjVHoLgFmBXMRN/IgqczwZU54ZJniNwpgHMz8u33Tqan31XJizWJ0TII3EjMvhoB9R/ZWjB1UYZbUIlvZqzEFXHdXvwbl7o/ZZkSibIuumqnndG4OHZZhIBI3usUg/nD9fLwqnyN5x7qonEMZepHFxueBHC6jOvCcoOQcgUOIVDPypASsQp+VJcbHjHIY3Zmp9T4j3B7J8QLeZHt/rqaU4dBaCi23lDTg2eTLlB0TMTqGDLdS1rpfUAo6jJfwqSp10ai7MuYRosxPVfgSiboC6aE1YZNf8EpzA8GN3dSxmJ5Y7t5NPCZpAwINDQGt7LE588mQbBHjZOCBsucU511FGLXUzQ06JpQKHFpUMjZ4+RL+ingdC7I7oilMbrhTRjSRmx6QbG6d79LhceW0r0lH3VldHySUTZEoBAX4BRSFpBCziRgkHdYpDmAmH6+ThMGVhmPdVU3JiL0436Si1EIhMmyiye7OboIIIcAoynOEsREnZHToZJlaWqWJgEbzs4KandHqNRwAutBuib8L8XnTyA26txGui24g8LcAijxcdUSgEB0BYVNe8J7osD2mN3dSMLHFp64ozI8MHdNYGNDB2WKTZn8sdkek8CFZWQCyoBBDgE1RWzDNsdEabI15f26AsSHLiij72UdSWi//wCIUsDZG82H9R7LLZl/dHg7heyzFON+NuAbdZODTwtwC3WysigihxKvwdugLIDqhkLHBwRIeBI3usWi8QlHfrweG7jKeylkEbC8qRxcbnyCFbgArKyshwCYpXWACqXkwNLu/wD1p0YZSc1/Mf6WquqPiJi/hDM6J2ZqmYHt5se3+uBTt/ItwBunDg08LcGo8N+J436AOsFYbLniMfsqiLmwuZ10cPKga39VistmiNHrt0W6gmKo3CqNYIz/AH40VA+qd9liNSyJvwkG3fojlcz0lDXdP6mtVrdA34Fq2QdwKC7q3E9YHkBUE3KlBR8LlXQ8qcjpo4edM1iKrZeZKXI+cOITFUC9lnOXIo4nyGzBdUuFAOHxB/RYjXMpWcmHf/SJvqemNwvZycwjdFl9kRbiOkpm/Ei6tZAIarbyD5l0w6qN/Nia9YtHdrZP06cGj9UiqZOXE5yPyQTV4dMyZTQszPl1AUE0tR4aZuRvuqqsjpG8uHV57qS+bXrpPxJcju6BLHKdjT42qysg3qAt0EJvC3Xfy78AmFYY/NG6NVMfMhc3pw+Pl07fvqsWksxrEfkQgmKRt2J2abwD0t/ZS13Lby4P3V/Iof47VJq4lAi1llVvJJssy36NunZWV/IuiVdXQcmFYe7K8IizlUx8uVzOLW5jYLLlAaOyxR+aYj2+RCAQCY1HVuZ+jf8AalqHP8I0HC3Rbi8WsoWckPkPb/Z4gojyi1AW6b2R4X8oq6L1mTPEUWhRlU85annNZ47rFmWlDvfjhzM9Q1X91M/O8u+RCATAvCDZ+y5UtS7QL4aGL+K8fpqmVlHFsy6/5WD/ANoJ9fTu/khPnhPpj/yjJ7C3FjbKODmVYjWIRPYwe3f+/Q0jYrRW+aKc5ZlHq7VSNFk0rO4posmFUj89M0+yxVl4g7244Mz8Rzvsql2SJx+yd8gAmsQaO6sQLhMZBH4pjf8Asv8AlYAMgbohLh8nqbZf8bTTfwZFPhc8OtrjpaLalYfHzZsztm6qifymyVTlBWujcS7UHdS0PMHMptR7ItINiiOjf5golfVqpsuVBFznJg4BArCX3ic1VjM8DhxwdtonOWJutTlHjfzAExicWxerf2/+0aqT6dP7IT1HZxTa2pbvr/dCppZ9JmWPuFPhzmt5kJzNVyFBidRDsbp8lLWDXwP/AMKWJ0TsruANle6poCyl03foq6YaQM2bwjmfEbsKGIh4tOy6ENPVsywmzk+N0bsrug/KHoITmrKsqsgOIWDu/ELfcItzAtRFjwwxuWmH3WKn8NoRRKv036gEAoo8xspD8OLD1f6UNLJLsFHhvLF3f4TpZ6ceGL/tf83J3YEMWid/EiCjrqX+X4VVU8cnijOqItoeAlNsrtkeEUJc8NG5WJTCljEbN1foa4tNwjVxVLbTjxe6fG36SrdFvlyrK3C3ELC3WqGruqluWVw+/CiFqdgWMHVgRTgUU0q6zIdA4AJsd14Wqnp53m7BZfC09OM8xuVNi52hCdX1DvqTa+ob9SdV83+ME4AbcGvc3ZOeJRrv0NNjdUlTyZOZa6qql1RJnd5F/nLdQVG7LM0/dO3WIC1Q7hALQs/sFi5/EH9keBbdZU4J1u3EHgExl01vYalQ4bLJ69AoaOGDUBVWKhvhhT5HSHM4/nMBs8FP3WJj/wBQeDBZjf7BYt/G/TqsrcLcIYnPPhCf4dLqhqGMPidb9FzmBua+iqpn1LuXGdE/C6hovZEEaH85j9QT91in8bgz0N/ssV/jeUAn/g0oA+rjndtdCRzdQUzE5suR5uE92Y3/ADlm6csV/jcIjeNv9gsVH436I+S1T+KnYfa/5/EPEE/dYp/G4Qaws/ssXH4jf7dV+gKEc2F0f68IYDI7KqimpaRnj1cj+U3RPyNOLvaFJusTP454URvTsWMD0HpsrcQgFTuMbw4KWIc6zESzDor/AFlSSOkdmdv+RnjfrA8+gbmnYPun7qvN53cMMdmpgsXH4QP38kBNaicgVLG2mi+Jm/RTzumfnd8gOscD5APE9dlZW8u/QFhTb1DU5VDs0rj9+GDuvG5qxFmanKPUGpsRK5YG5WYD0qhojK7mTbBYjUuqHeH0j5EFXV1dX4jgeA6CFZW6LKyt1iNx2CbSTu2aUMMqD9Kbg0x3TcEPdydgfs5VVJJTnxq/RZWVkFgzfG532T3ZQSjqeGDPtI5vuFMzPG5v2Th0AJsfuo43PNo23XwGUXnflRko2aNBcVLI2JoMoA9mj/tOqnshBqO/ZT1L5t9vJurq6vwPkgcbK3m09JLP6Am4NMdyEMEPdyqaCCmbd7tUxhkdlYqbCGjWZMpomelqsOqsibLC4FWVuoLCGWhc73Va7JA48cOfkqGobqpZkkc1HixqhomhvOqTYKfFTbJTjKE57nG7ioJBEc/fsubmkzyaqaZ8zszuNkRbfzCFZWVllVlZW6g0lNppnbNKGHVB+lPw6obrlRFt+FFhkcsQe9DC6cdkKKBuzVWODpnW4UVA6oNzso4mxNyt4VdW2nbc7qad878zlhlIIo853PRcJ0zG7lMqYnnK13Gtk5cDneQwKkZkpmhYs+0Qb78WOyuBQNwHBYtHllze6PBjbqkp2RM+In2GyrKx9S6526gwlfBGNmeTRGS2jUT1WVlZW8gRuOwToJGDM4cGRuecrQocHe7WQ2TcHgG6bhlOPpTaSFuzQgxo2HRi1K0s5o3CaLmyhZkYG8Kh/LjLkdSqDDnT+J/pTGBgyt4VlY2nbc7qaZ0zszlSx8yZrUBbThXVHIizBOrZ3fUjK9254YOGtLpHlOrYG7uTsUpx3WI4gydmRnkQML3BoTgG+Edliz7yBvt0YfJzKdv20WKxZog/2RCAVDR8w67LE6gF2U9th/8AaJv0MZmNk2npYP4519kcSji/8eOymqZJzeQ8CrK3UGkptLM7ZqbhtQfpTcHnO6OCy9ip6WWD1jhhcAll8Q0CbBG3ZqsFjUmjWKKMyOytVJRtp26b+Tij8tOVQR8ydo44tJlgt7rD8N5n4kuyADdBwrK1tO37qWV0rszuGDRZpS7244lSyVDQGI4ZUD6U6jnbu1OjcNx5YWFRZ5gfZO1KqpOZM53Rg0vqjUrOZG5iexUtKZHWVXXNp28iDf3RNzc9EUL5nZWBOMdCMrNX/wCk519T101OZ35Am4J7uTcGhG5TcMpx9KbSQt2aEGNGwVuiaJsrC1ye3K4hYPFljL/fjikuec/ZYNBoZTxrMQbT6blOxqQ7NTsWqCnV9QfqWEGR4c9x441L6WLBYvE5/GambM8F+wW3CtrW07fupZXSuzO44M0CInqsCnQRO3an4fTu+lVuF8oZ49vIaFhceSIv91VScuJzumjl5UzXLYqal/GIVVU8lvKi37lHiyNzzZoTMNyDNUOyhSVzIm8umFvui6+/kYNDYGXquFnbe1+LjYXUhzvJVLHy4mt4PdlFypHZ3lxVFHy4Gt41snMnceilxIU8eQNRxqTsEcWnKmmdM7M9YVFkgB9+murBTM+5UkjpHZndGHV3w5yu2KZVRP8AS5Zh1yDwG6PUFE25sgzlsbH7LF5bNEfVRy86FrlWucI8zVIjwpaFrRzanQKTEQzw0zbBPlc83cfJGpsqWLlRBnGuxIwSZGBOxaoPdOrp3fUjNI7crDmF9Q3jiMnLgKooubO1vHEpOXAVTR82VrEBYcKqTlxOcj1tFzYKKPIwNVuFuFXSiojylSRujdld1B7hsm1czdnFNxSob9SbjMw3CosT58nLLeOIScuBx6wFhkGeW57J2pVbNzZi7qweezjEe6c3O0tPdTRlpsUGFxsE2GOiGaTV/t7KaodK7M/y8Nh5s4+3Emwup5OZIX9GCxeqTjjUvpYsFiu8yccal9LFg0WaUv8AZWVljMmWIM9+oMcdgnRPYLuCw2LmVDeNZUfDx51DK2Vge3jilDzW8xm440FIKmTKU3BYBvdDCqYdlikEMEQDG6njZYLAcxlI445LZrY+oBNCoIuVBf3VdNyoSeuKQxvDx2TXh7Q9vdVtMXPzN7pzmUTfBq//AEpHlxufMwaHLGZPfjiUvLgP3Vk2nld6WpuG1DvpTMGlPqKp4GwMyN4bKvm5sxIWFQ5KcH3VlZYlJnqD9lg8OSDN78cYlzz5fbowijZIwvkF02mibs0INAWNyXkEfssDiuXScLLHJNGxrDK3kPyO2KGvHFqHlnms24YHFZhk9+NRRR1Fs6bhdMPpQoKcfQEKeIbNVgOOKT86c27cbIcGhUkHNkDU72CxWbNJkHbyMInzsMJ7JwJYQFLvqncWtui3gB0tbmNgoI+VGGDhZSU7JfWLpkEbPS1W6cTrwwcqM6qNpe4NCjZkaGjhK7IwuKJL3391Tx8uNrFZHQXU7+ZI56DHHYJtJM7ZhTMLqXfSqSn5EQj410vNnc5YVFy6Yffji0meoI9uGEV2ccl+/F7A9uVyrqQ00mXssGeDT28nE8SEbeVFv0gKyY1YdFy4+Z7qeURMLynOLjc+RSzmCUPCDr+IKvgs7OO6kbZWRQKzaILMndGEwc2e/t5BcG7qXEIIt3Kqxd0nhi0COu6wiLmVAPtxxaTJTn7rDY+ZUtHQKaIahoQY0bdOJVgp4rDcpgL3Ae6jZkaGjg42F1M/O8u9+DHljszVQ1jaiPN3Re0Izxjcqt+HqI8hcLqlq3Ukn2UWK08g3sjWwD6wjiVOPqRxamHdHGadHG4PYp2Os+lqqMWnl0GivxAVkEFSwcxwaEbDwhYrPc8oeVhVRzI+UdwpIuazIp2WQCf0AJw6MGgyRZ/fiTbdSV0EfqcpMahb6RdSY3IfS1PxKof9SdI93qPRgUNmOk447L4mxrDpRFUNcVfrJA3VXisUOjNSp5nzuzvVHIyKYPfsE7HYuzSnY97MUuNSSNLbdDXubsUXE91fzmq+q3UbLqhg5TM53KnlETC8p7i92Y+VTzGGQPCa8OAe3YrEYP5gTxbpanHjGwvcGhRRiNgYO3HHJrBsQ8mgi5UDW8LhYlJzKhx4UeMOiGWTVDGqYp2NwDYFOx4fSxOx2Ts1OxmoKOJ1LvqUlTLJ63fNNaiyyhiuqWlzP+ycVilTnfyxsPMwip/kO/ROaHDK5VMJjdlKI8jBoOZPmPboxCbmzuPkNNjdOxmoO2idiNS7606eV3qd+QgcQ7RZrqC+yiZymZe6rqnkR6bnzWuLTmCpqgVEYkH6qsp+ay43Ce1EdeDwcuDN78a2XlQuejr8hYoRlCErkrlLlrIsqsrfJ3RcgeETLrD6e34rv0TnWu5yq6gzyF3nUFX8PJrsV9wsQpcpzt2Kc1W6YYzJIGDumMDGho44zVtc0RMPmWQahGhEhEhAhAvh18MjTIwIwosRYi1WVlZW8u/UAmtVDTGV9uyPsFilX/JZ+vyGFVlxyH/onNDhkcqmnMTspTm2VujBIM8pkPbhU1sVOPGVV4pLPoNB5NlZWQCDUGJsSDAtuyMrgue9GZ/uhK/+pc1/uue/3XNcVmv2RAKMaMaLUQreXZAKytxAUEJkdlCiiEDOW1V1V8OzTcom+p+QBsbhUVV8TH//AEFNCJm5TupYi02KIt0YYxtPTZn6XVbjN/DB+6c5zjc+VZWQamQl2yioHndNo2N9RRMLNkZb7IlZQg1vstE5oduuWh7ORa32Ra1WVx3WRp2Tqc9k6OyLUQrKyt5gCjZdUlMKdtz6ippWwszuU8zpnl7vkqed0D87VFM2ZnMYqqn5zbjdSMsiOA31VRVvn329vLsgFDSSSbBQ4a1ur0BFFsufF/UjyX/dGliPZOo/6U+B7N1mWdZ1nWZBuYKXwrMs6LlGC42amU8wTo/6gn0oOykhLd0QiPMATGKhpeWOa/8ARPcB4nKtqzUP+3ylFWOpn/ZNcHDOzZVdJzBzGbp7UR5gCgpZJT4QoMOjiF3KWtii0bqpa6ST7K6usybO9vpKhxE7SJrmvFwqmizeJm6JssyzLMqGXN4CqiHOxErMsyoIwI83uqqo5I+6dUvd3TZ3hMmjl8L1PQ/UxObZEeTZAJjFQ0Y/iPRN1iFbzTy2en5bD67kHK/0oH6mqso8/wCIxPYiER5Fk1pdoFSYX9UqlqYqYWCnrpJd9lmWZZlmV1mWZUtUYX/ZA5hdYhSknOwJtJMfpQoZj2X/AB0yhopo3hysqqhkc/MxGinHZOp5Ru1YefwQFitw5pWZZ0HKGsdH/ZOiiq23bup4HRGzkR5ACYy6oqP+Y/ZE3WIV+b8KLb5jD8Q5P4cnpV+4VXR5/wASNPaiOuygp3zuysVPRRUrczlV4oXeGJOffdXV+hkb3+kKPDJ3b6JmED63JmHwN7JrQ0WHEvaFzme658f9Sa9r/SUUHBaLRTU7JhZ6kwhp9BUuHTs7XRzNNig9RzFhu0qKeOrby5N1V0boD9kR0gIBMYqSiHrkR1WIV9/wovmqDEOT4JPSgdMzVVUYl8TN0+OyLVbhZWVlR0b6l1m7I8jD4lVVz6g67IuV+Mcb5DZguoMHkdq/RRYZBH2ug1rdk+ojZ6in4rA3bVPxj+lqoK19Q8hyKlkfnIJV1dXWEyWcWKRmdpanXYSEJ5Bs5MxCZvdRvD2hwUuINhkyPCjrYZNnKSCOUeIKfB+8JUsMkJs8Jr7KkrGTt5M6raI07vsiOFkAgEyNUtGGeOT9le6r8QzfhRbfOUOIOpzldq1NcHjOw6Kopmz691LAWGxRYrKysqOjdUvyhTSw4dDYKeofO7O9E8aejmqPQFTYGxmsxumxMiFmiynr4IvU5TY0f5TVLXTybuV78cG/ilFSOu8lX40cnLmaeGIsyTnjhFTcckrFabOzmDccIquWL0lQY12lCa+CqbpqqvBfqg/ZEOYbOVDWNqWfDzqrpXU78pVlZBqihLjYKnpWw6ndONhmcq3EDL4I/T89SVr6Z2myhnZUNzRqSNsos9VFIY/7JzFlUFO6Z+Rqe+LDYLDdTzunfneieENPJM7KwKkwZkfim1KvHE250CqccjZpELqfEJ5/UevBYrML/dVb+XC5yvxugbaqB2eMOWNR2LX8YpTE8PaoXtqIw4d1XU3w8pb24skdGczSqTGyPDOpqanr2Zh+6qaSWkf4v3UMjMRh5T/UFJEWOyuQYqekdLsoomQizFLKyFueRVdc+oNth+QQzvhdmYVSV8dTo7RyOmhU9FfWNcrWyp4mUEJlk3VVUOqJC9yPCjwt0vjl0CjZHA2zdAqrGms8MOqnqZZzeQ+QBc2VJByYWsWNzZYxH79WEvz0w+yxpl4L+3Rg9Zy38p2xWI0nxMVhuERlNj0U1XLTuzMKgq4a+PI/f2UtLJQyiRmyq2MqYxUM3UFD9Ui+zVVVrKbTdynnfO7M8/kQNtlR4qR4Jtk0hwzMKytLg47rFObJa+yLUIy45WqjwwReOTUqqrY6ffdVNZLUeo6eVg9Nzpsx2atN1iFRz5y4bdWAvuxzViTM1M/ovbVYdW/ERa7hYxR2PPZ+vQAmZmm4UFQ6ohyuaoYWQCzUdBmcqvFfog/dE31P5LTVclObsVNXxVGmxWoU1DHJqzQqipY4N/Uq7Esvgh/dOuTcq3kgZjYKhg+GiDO6xSs5MOUbnrwF9pHD7KbxxlqPRSVBp5A8LM2ZnuCqqnMEhagEGqGmfIbMChw9jNZNStToFUVkVNodT7KprZag+I6flNLiskXhfqFDURTi8ZX2Klw+N/o0U9JJF6gi1EKyt1YVTXPOd+iMlhcqsqTUSl/XhDrVCzqUWeRxsrLDam34TlWQ86P7qCkkl9IUOGsZrIboaDK0KaaKAXkKqcWe/wAMWg/LGuLTcKnxd7dJdVDUxTDwFaqWihl+ymwuRurNU+JzTYhFqsrK3CnhMzw0JlmNytWJVX8pv6+Rh5y1DVmVUPxnf3VlZBqbGToFBhsrvEdEIWj1arXYKWoih9ZVRi7naRCyc4uN3fl4JGoUGKTRaHVQ4nBL6tE2x1aU5odo8XUmHQP20UmEyD0aqSklj9TVkWVU0HJb91PPym3TruNyrKysrcLKypdJWq6qheZyjppH+lqjwqY+rRR4ZCz1G6Y1rNGCyJ7uUuIwRfcqfFJZNG6IknU/mkdRJH6SocZkbpILqPEqeT7Jrmu9JXiToo3+pq+BgvcCy+G9nKfDZpTcEI4VUDsjh1QPpRoZx9BXwc39BQopz9BQw+oP0JuFVB7IYPL3IUWFBhzF6+Hj7kpsUTdQ1XcnOaNXFS4jTx97qXGHn+GLKWokl9Z/OmyObq0qPEZ2d0zGX/UE3GIjuE3EKd3dCaF2zkHt/qV/Yq7vdXcvEiT7ovaN3I1EDd3p2IUze6fjEQ9LU/GX/SE/EZ3906RzvUf/AIHmK5r/AHXOf7rnP91zn+65jvdZj/8ANf/EADsRAAEDAwIEBQMDAgUDBQEAAAEAAgMEBRESMRATICEiMDJBUUBCYRQjUDNDFVJxgZE0YrEkYKHB8NH/2gAIAQIBAT8B+vyn1MTN3J90p2+6deo/YJ17d7NRvM3wjd50brUfK/xSo+V/is/yhd5whepPcJt8+Wpl4hO6ZcIH/cmyMdsf52Sqij9TlLeo2+gZUl4md6eyfVTSblBkj0y3zv8AtTLNOd02yH3chZGe7kLND8r/AAiBf4RAjZ4UbLH7FOsfw5OsknsU+1VDfZOppWbhBz2qO4zx7OUV7P3hRXOCT3wmuDu4/lpamOL1lT3to7RBS3CeXcprJJNgorTO/fsorIwesqO3wM2amxtbsPNLQU+khf6mqWzwu9PZS2WRvp7qSlli9QTKiSI5aVBepG/1BlQXCGbY/wAlUXKGH3yVUXeWTs3svHIflQWqaT1dlBaYWerumRMZ6R9KWg7qe2QS+2FPZZG92d0+J8Z8QVPcZofdU12il7O7FA52/jaq5RQdtyqm5yzdtgo4JJjhoVPZveUqKnji9AWVrCD0HLP00kDJBhwVTZWnvEpqSWE+IKmr5YPSVS3SKbs7sf4qoq44B4yqq6yS9m9go4ZJjhoVNZgO8qZGyMYaESnSp1WzVjPdTV4jeWYym3P/ALVT1PN9KbIgfo8LSscHxteMOCq7M13eJS08kJw4KkucsHY9wqasjqB4T/DOcGjJVbeMeCFEyTO+SqSzk+KZRQsiGGDg9+F3cNR7BVXPc3LRgK3U5MwcRsjQSuOp/b/VfsU/oGo//CmqZn7uVNXvjOJO4UMocMhA/QBqDeuWBkow4Kss5b4okC+J3bsVRXjPgm/5QIcMj+DqqyOnGXKrr5Kg/hUtukqPwFTUMdOPCO/F7sIO1u//AHdcvtl6qKl2cR9lTuncTq+E6kld3K/QPTrdL8Kamez1BW+o5buW7YqN2UPOA6SER01dAyc/lVNHJAfEqO4yU5xuFT1Mc7csP8DXXVsXgj7lOdJO/v3KobRjxzJrQOw4kqqkwFTxaG591PjHcoyNb6GqJ73k5KMch3TmrxDYoVczPfKJgn/7Hf8Awos+6HmjugMdTuk7p0TZBhwVdaCzxxbKKZ9O/LVQ3FlQMHs765zg0ZKuF1Lv24tlBTyVD8NVFb46cZ9+mQqOPnTfgcKgZCbFndRyeLSB2Tnvad1lsvZ26liLTgp7FI3Ct8jnx901BBAIjt5LB0boDHWd0FhXC0tl8ce6c18D+/Yq33USfty7/WSSNjbqeq+5OnOlvpVHQPqT+FT07IW6WDpKqX6QomBjcDg9uQnjA0oDBypG+JaUCHDS9TRFpwqkYVrZ+1lBDhlF3kNHQSmt6HdI411vZUj8qenkp34crbddopv+fqpZWxN1vVdXuqXfhW+3Gc6n7KONrBpb1Eqccx7WcHysZ2cUHAjIT2rSnjIysIhZ1eByqI3Pl5ShiEbQ0IDywzoceA7joPSOiso2VDcOVVSvp34crZdMftS/USytibqcq6udUu/Ct1tMx5kmyawNGB1FPdhU3jnJ+OE3eX/ZRuLP9EHtK0rHsiE44VRP9rN1BC7s+T1IBDymDv0uGVhB2EStSBPDHQOmro2Tsw5VVK6nfgq1XP8Asy/TPeGDU5XCvdUOwNlbLdzzzH7JrQ0YHWSpn4VsGYzIfc8JP6x/0QCLAVqlb7ZRqcex/wCE+fUMsC/T1M2/hCp6OOHbfycI8W8CeJTjngBlYWOpqCPH2VZSNqGYKqKd1O/SVarjzByZN/pCcd1c7hzjoZsrdQGpdqdsmMDBgdZT1UbKiGIG/wCnCbtUj/RNQ81vdFvwnDHEHC5iPdDuOD+ARyEEekBN2QCPH24V9GJ2flPa6B6tteKhml3qH0d2uH9mP/dUVI6pk0hQwtiYGN8lyd3e8H4/8qnGmJo4SO1Vn+gTUPNb24Hfv0NGU4YCYUU48GjgU6ojbuUaxvsF+qd7NXPkXOkXOkTah49k2qHuEJWO2PH24OCudAJRqbuopH08moKkqm1EesfQ3Ot/Ts0t3KjjfO/S3cqio200ekLHkuTYtU/fgVTnXK96aUPLazIQGAnEZTTlSD36GnBT/wArVhOOeGMKSpjhGXlTXkntEEyWabfJ/wDCZTzfGEadw9RQpQdyhSNX6dqbF3wv05Oy0ELCDi3ZCUe61jCM5c/DduDmZV0ocfuNVvrDSy/hMcHjUPPqJ2wRl7lPM6d+pytFAIGcx+5QRCwsdZCqnGFwlHsmuDhkIjKkotLf2twoJtSB6NS1oHpYMIodymN0p23HCDCnDK0BaVjCuN15X7ce6goJ6rxydlFb44dgmtHEdDNuBCLeDhwCaVPGHdirhSGB6s1d/Yf/ALefdaznP0N2CtNFzXcx+w4Rd91haVhYWOkqoZrYWlWqfLDEft410JjP6hn+6hkDxkIFPkDBkoOkl9PYL9OPvOVyWAdgmOwcHsgejmFZKZhA8JDlDuUBjprpuTCXq30O00vclBe626iVH6eJdjg5BDgRlVtNzmYTg6B6t9WKmLPv5t1rOTHobuVTwuqJQxvuo6dsEYY32WEDpXMKachaVpWEel4VG3RUvb7ocCA4YKgBhe6L4WvChZzzqeO3t0PDSPEmOzsh0h+lcwrmFAat0AB1XCPmQFauWBp2TJA4ZR3R7rPUwgIu913O6A754EZ4Di6LsrvR/wBxqttWaaX8IHIyPLkeGNLiquoM8heVZKPlM5rtzwAUjVhNOAucmu1IhELHQVU/sVbZfnorv23tlTgXNOnumN0t0jpeNLSWqM58QQ6A0laSFy0BjrKlh0s/bQbrZiQYUfhdjUhLL+FzJNOQE6R4GyE0vwvFhO1exXf3Kx24bcDlBYQQGUG4RU0YeCCq2nMEpCs1XzY+Wdx5d6q8fstVupf1M2PZMGBjgDwDVKMDhAFhFqIR4lV9Pzo+26t9RzosHccXsD26XJsHh/PymSEO0SdTgm9+gOITXZ8o9+BibnJCdRxHxJlOI9inRZ902L8rThaQtI6scAMoDCJwiUVd6Xmx6huFR1Bp5g5MeHtDh5M0giYXn2U8pmeXFWql5EOTueIPAFEZXICaMKWQ5woXFw7pwRHQ5TxPgk/URf7hQzNmbrb0SRh7dJUTj6H7jpPcYTfjpYcHy3BDxBD4WVjygEBlAIlE44vblXCn5MpVkqtbOUfbyb3U4AhCtlNz5u+wQ6QVlAoFPh1d0xmhFEIo8CnhRtMU/wC37+yHfoczJDlHI/xtG4UU7X9vfgThdzsgMcMY4sHfyCcLPE9u636Mo9bR2QCLkT03im5keoeyopzTzBya4OGR1vcGDUVVTGaUuKtFNyodR3PUOAKysolakSj0OU+R4huEKjU5rW+/foKovHJJInwA9lHM5juXN/sflasyY+EODNuBGVoCa3HHPAnC18HDhngVst+AR6gODdkSieqRmoYKrIDDKWqz1PNh0n267xUcuLQPdUcPPmDEwYGPIBWVlErPDKPApxRbqKomjmux7dFZPobpbuVTRcqMN4SxNkbpconFh5T/APlA8G7eRnhjCaeDuGeDkOG3WOHsiesq90+0gVoqOVOB89d1m5s5/CsdP2MpQ8vPW5Q93OVINM0g/wBONRVNhH5VJC57ufL0PY1/Zy9OyZ36nOROeg7cA5bot4BFeyHkDhnyayHmxFqxy3qkm50LX9NTLyonPROtyo4eVE1n0RTlG7Q9aBq1p8jWDLipq7I/Z/5VJTOmdzJNkB0vacdk2TOPymyY3QOeJ4Y4hO24g4QOUStvJHl4WE4K5w8uYqxT5aYz03ubTEGfKtsPNnAQ+hKKcp847LnTvIjiO6kjip/67tTlBA+oPMl7N+Ezbt11Y0x62+3dEB4woHFvgdwyi7pCJz0Ao+Tjy8LCwnK9Q5aHK1TcqoH56bxLrnx8KxxeqT6MpyfumEQjw+typ7f4uZNuseRV/wBFyb2CwcoOWc+SAtK7DqPDPDKx5ICwtK0p4Va3W0hDwPUD+ZGH8ScDKqH8yUuVsj5dO36MlPcsue7SzdQU7Yu+54Z62lSu5hawe/8A9ccIHygUendDtwx5QCwg1aVJ4QmvOU9TQhyrGaJSFZ5ddPj441r9EDimDW8BRt0tA+iJTypCSOyFVHA3AXOqZfQF+iq3+qTCbb5R/dKZSzN/uJscg3cg3i45UkvLgL1RSNe8/P8A9dDs+31gTWrSpOzVG45ThlcsDunFOCvEWmXKsMniczjeX6YMfKtrNdQ0IfQEouTpgnTt+URNN2j7JtpnB1ahlGOvZs7KFfURf1mKGuhl7Z6Sc9gqp2iPA3Krf3HMpmqWka9oDexGyirdB5dR2KznuPrQgF9vZQ6tSKDWtTiiUUVfGdg5WiTRUj88b670tVkZmfKHnkqSTSmsfP32CbSRDcZRhh92hPoqd/sv09RD/TdkflRVYJ0vGDwlooZPbumtmg/7mpjw8ZHRUTjn99md1QxOOaiTd3/jhJEyQYeEaAs7wPwjNPTuzMPD+E1wcMjoB+kHQCmuWpZRKJR4FXdmqBUztEzXcb2/MwCsTfU5BY8wlEqSQNGSo/3ncx/pU1wbH2Tq3mnu7H/78KKmp5T3lX+FR/5iv0EjfRInU0/3+JQSub4XoHPDR3yEOEkga0uOyoojUyF79ulzQ4YKbTvhP7R7fCDj7hZ6M/RDpBQKysrPRXt1QOQ7OULtUYPC7HNQVYx+0SgmkIJwWFpR6DwJTnp0iqaqPGHHK51RUeCIdlBZxvMUyhp2/ajRwH7U2Dl/0yh+eBaDug0s226D3VRDzWaFBC2Bmhv8JnrqBmNwT/UqE6oGnhcjmocrMP8A0/EOwtSaU3V78MdDnYUk2O6muTR6e6lqpZj3VHaS7xzJkbYxpaP4webIPCVL6yrYc0zeFcf33K0f9OOrKys8CppmxjusPk7qspJXDwtyuU/Vpx3VLyqVvMkacqK5QS9gUO/8YPNdsqj+oVaf+mHCu/ru/wBVaf8Apm+USo/3apxP28TG0nOEYmO7EI0EQOpgwmjAx/MuVR/UKtH/AEw4V4xUOVoP/ph5RVONNTIPnH8+89lP/UKtP/TDhchipcrKcwdWOmb9udsn+3CaqbGMqmqKiqd27N/i8fQynDSpfUVaxilbwu4xUFWM/tuHlFVDBIwtKE55Xi3UUbq6T/tCYwMGlv8AOVJxE5P9SoRinZ/pwvbcTAqxO7ub5JRKcclSl1TJyGKGFsLAxv054DyCPKysrPl44nhcHaYHLcqFumNo4X1npcrM/E+EOolGTCMqdIqqpx4Y91QUwp2eL1H6EhYWFhY4noPRnhnpys9Ze0blOq4W7vCNzph9ydeYBtlOvjftYm3z/MxU1XFUjwHryiru7EOFTt1ytbxvLNUGfhUL9E7Sh0Ep8mFJUhvdxX6tzziJuU2nqX93nSo4DK4hh7fKNLzJtMHbHuoKdkQ7eVhYWOA8knjlavNqKuKD+oU68wjYFG9t+1qpq+epdhjcBOeGN1OVTeXHtCn1UsnqctXVRSmKZpCys9d7fs1WpmupbxrWa4HBNOlygfqYHdEsmlSVL3u5cPcqC2D1T9ymtDRgKVmsaVow3S1RxtjGlvRnPmZ4ZWVlZWeskBOqYm7uCdcacfcmXGnf21IHO3CtuckUpjYnXWoPujXTu3cqRpbC3Vvwra9tMMfcpZXSu1P4UlI+pdgbKGFsLdDVc6wyv5Y2HRgpsEjtmp9NKxupzcDjQR8ydrfIKusmqYqxR5e5/EjIwqhnLkLVapdcA/CHB7sBTvfO/lxqlpWwNwOouwv1Ac7SxBnyh1ZWVnyS9o3KbPG86Wuzwe9rBqcVNeWN7RjKdeZjsE66VJ+5OrJ3buKL3Hc9FoqnB/JOyJx3U7+ZIXcKePmStYgq+4tgGhnqT3ue7U7hRUTql34UMLYW6WKqk5cLnI8KCm/US6XbJtFA37QhG0bDheS5wbGwJtDUO2YU21VJ9lbrc+nfrf5EjtIyqh+uQlWaLTT6vnovEWifV8qyy4JYgUSqyox2CoYjjI/5/wD4gMdDnaRlOnnl/oDt8oUT3/1n5UcLYxho6M9RICdUxN3cE65U7fuTrxANsoXqL3BUFVHP/TPC6TmGLw7lOqJXbuKyrJF3dIpZGxtL3KsrXVDvx5NqZqqAfhV8nLp3HjaI9U+r4VwuQi/bj3RcXHJ4UVE6pd+FFE2Julo4XiTTCG/PG2VUdO4mRC50x+5Nq4HbPCD2nY+ZcZdEJXqcqePlxNZ0XqLVGH/CoZeVMCmuVTUBgVLRumPNm2Q7bdEsrYm6nlN11h1O7M/8oNx11NQ2nZzHJ18/ytTr1MdgE661J+5OrJ3bvKL3Hc9MEronh7Ux2oZV6l1ShnxxtceinH5V6n2iHGitzqnxbBNskQ3JTbTTj2TaCnb9qvAjZpYwcbJF2dIr3LhjWcYKp0LC1m5ROeFDQuqXZOyjjbG3S3jenEyhv46g5NqJW7OKZcahv3KhuvNdy5N/IJV5m2YrbDzaho6amLmxFq9LlFU5iBVPT8w8yToc8MGXFPuWo6aduoqOifK7mVJz+EBjyL1N3EXVgrluxnHFo1HATBpYAquTmTOdwY3U4AKNuhoaFXycydzuNFHy4Gt6Kq2Gol1lyFli93FNtNOPZRQtibpYFdpNc+PjpoKI1Lu+wTI2xt0t6LhQ/qRqbuFJSzR+pq0nriyHjCHW8qtl5kpKscGAZeq6wcqc/lWshx0uTONRXOJ5VP3KZbzJ4qh2UyNrBho8knCq5ebK5/GhtjZ4+Y8ptppxuE2hp2/YhExuwVxeGU7uNsi5lQPwqyXlQOcjwtkXMqB+FUScqJz0Tk8KSPmzNb5DjgZUz+Y8u+eOFhUdSaeTUEx4e3U3qLQd06khdu0J1spnfanWaA7ZVZaxBHzGu42+PmVDR1lV8/LiK9TlSQ8mFrOq8Qa4tY9lTS8qQOUb87LUGjJTpJKs6Y+zPlQwMibpYPLuU3KgP54tGThQR8uMM+Oi9S+mPjZIuzpFepcRiP542SL1SK8S6Ygz54YVmizKX/HUXAbpsjXHDSrhJogceNFT/qJNCmidE8sdxtlbynct+x411V+nj1BG8THYBG6VB91bJ5p5CXu7DovEw0iMcbLF4nSdRRKu0+p+geytVPzZwfYdb2h7S0qoiMMhaVb6kGLB9kGuqT4vSmNAGB5l6m1SCP442yLmTj8cHTxs9Tk65U7fuT7zCPSMqondO/W7gFQw8mENKusnMnP4WFhW2LlwNV2k1zafjjaYtMOr56LpVPjcGRnCNRKd3FFxKs8eIy/5V5k7Nj42WP1PVxo+ezU31BYxxtlZzG8p+44XmTL2s409XJT50e6NyqD9yNbOfuRqJTu4o99+Nth5UIz78co8CVUS8thcpX63Eq0U/Kh1Hc+ReqbaUKkeGvGdlGhxJwg7hnpcdIyVPKZZC88YppIs6DhOnkf6ndVsoC482TZPdpbqKkdrcXHhEzW4NCGGNwp38yQu4AZKhZoYGouA3RqYm7uCdcKdv3Kqm50hfxpI+XC1quUmuc/jjbY9EA/PC6Uek81m3Fjyx2pqpKkVEer3V2bibPk2+3l55km3SSsolXWo/thUdP8AqJgxAYGB5E8QljLCpGGJ5aVb6nmMx8Jhzxwsd+GE3ouk3LgI+fIDCdlHb55NmqltLI/FJ3KCucmiAj542yPXOPwq5+iBx6DUSndxRcT00FKZpMnYJx0jKkdrcXcGjJwo26GhvBzQ4YKrKY08mPZaShC87BUnPgk1BpVTStqWd1JbZ2e2UKOf/IULfUH7ULXUH2QtM6Fnm+Qm2Z/3OUFshi779GVngVPLy26ip5DI/JVmpeXHzDufKvNL/eaqKflSZUTkSm9GUD0XebXLo+OIBPYJlFO/ZqZZ5T6jhR2aMesplvp2famsa30jovEniDONnj8LnqtjMkDmjyACdlTW2SXu7sFFE2JulqqWOfGWsQs8nuULN8uUVqjY4Oz0FoO6wPPcsLZPcrpU5PLCoqY1EoamgNGB5U0YlYWFTxGGQtKtlTrZoPsgUOhybxe4MaXFSvMjy88bNDvJ5NZJzJnO4YVDHogaOFTa2yHUzsUbVOm2iU7ptmPu5Czx+7kLXAE2304+1MhjZ6R9U5yDsqWTCqKnQzUnuL3ZVqpORFqO58y70mtvNbuFTzGF4cFBIHtDggfIusuiHT89FFFyoWt8g90LXAN+6bRQD7U2Jjdh/Anjp7rThSgKuqOY7A2Vqo+fJqdsPNIz2KuNIYJO2ytdXoPLcmnyLrNrm0/HGki5szW/Q5WoLmBcxa1rWpZWfpMLSiFhPcrlV6Boaoo3TPDAqWnEEYYPOrKYVEelPY6J+CrdV85mDuED1Sv0NLinu1OLjxtNM5pMjh5mVla0XoyIyoyrnLnoTISoSIOWVlZ87Cx0kolVlQImalLIZHZKtFFy281+5+gutDrHNYoJnQv1BQTiVuoIFZ6LtNpj0fPCnpJJz4Qqa3Rw9z3PlZWVlEovTpQE6qaPdGqB2XOytZKyVlArUUJiEKlNqmpsoPug9ArPm5WVlZ4FSyBgyVXVRnf+FaqHnO5jth9DhXSh5LtbdlQ1Zhd+Ex4cMhA9Fwc6eo0s9lS2oDxTJrQ0YHmEqSdkfqKmurB6U+4SyelNhqJU2gcPUU2kaN0ImhYWEW5UmWFRSB3YrSCuS0o0wOxTqaVvpXPmi3Udxb9yjma/YoFZ+gc7CuVdrPLZsqOldUyaQoomxMDG/RSxNlbpcq2kdTvwrdWaPA/ZMcgeMFMyHbfzMqeuii91Nc5H9mdkRLJ+V+ml/wApTefFsMIV0490y5u+8KKril7BYWhaVpUsWtuEXFjlSyc1uVhaVhTPYxuXqWop3elqbIQfCVDcXN7PUNQyT0lA+cSrlXY/bYo43TP0tVHSNpo9I3+kqqZtQzSVPA6B+kq3V39t6aVnzaisjh9RVRcZZuw7BQ0Esvd3ZQ0EUf5WFhYT6eN/qCqLUN4k9jmOw5UdeWeCTZDv3C0rCwrpBj90KjqOTID7IDKwsK5vLpi34VFSc93fYJlJEzZqko4n+2FLRzQeKPuqW5/bKmPDhkeYXK4V/LGhu6GqR2FbqEUzMn1fTVtG2oZ+VJE6F2CrfX6vA/dNcs+U54aMuVXdCfDD/wAqGklqTlQUMcW26wtKwsLCwsKrpGzs/KcC04KtlYAOXIU6up2/cjc6f5RusH5VRcYJYyzHCkuMbIg2TdNuNOfuTamF2zgrmMTk/Ks3pcFhaUQqmgZN3HYpsk9C7DtlT1LJ25b5Rcq6u5Q0t3RLpHK224QDmP8AV9RXULahuRupI3QuwVQV+rwSJpQ8ieoZC3LlPUS1bsf/AAqW2geKVBqx0vkYzu44Ul0gbt3Ul5P2NT7lUO905xccnjocdkIJD9pX6eX/AClOjcz1DC3Wkhd+FPUyQHLFHeXD1tUVygf74QIcMhaVJC2QaXBTUstG7mRbKjrW1A/PWSi5V1fy/C3dEukd+VbbaIRzJN/qq6hbUNyN1LE6F2CqC4/ZImvygVnpqqpsDfyg2WskVPSMgHbdAdD5GsGXFTXaNvoGVLcp5PfCLi7dMglk9LUy1Tu37Jll/wAzlX0MdPGHNQUMbNAICxwwrxFlokUTtDg4JuHgFGCN27Qn26nd9qkYY3Fh9lFbXTR8yMqSinj9TVFPJCfAcKC8e0oUUzJRlhRblVdC6F3OgVFWCduDv0kpzlXXHT4I0A6R2ArdbRANcm/1lXRMqG/lVFM+B2CqK4mPwv2UcocMhByys8KmpbC3PuooZKyTUVFC2JulqxxmqI4fUVPdHntH2T3uecuKioZpdgorOP7hUdFBH6WrHG8/0gEFGMNA6K2PmQObwtz9dOPxxu9Ng84K1VGh/LOx4S0kMvqCms/vEVompnfCprpnwyoEOGQquldA7nwqmqBM3Us8CU+QNGSq246/BGoonzO0tVBbm041H1fXVFMyduHKsoHwH8KlrHwH8KnqmTDLUHLKlmEbdRTGPrJclRRtjbpbxklbGMuU9xc7tH2WHPPyobW93eTsoaOKL0jrvEmXhnwqWPmStb0kKZmiQtVmf2czjLEJGFjvdSROhk0n2VHUc+MH34uY1ww4KotYPeJQzS0rtJUU7J29lJGaOXmN9JTZA4ZCLlPUtiGXKrrnzHHsqWjkqXYaqSjjpm4b/APja8Ycq61FvijTXPhd2VLcWv7P3XMUrnVUmhuygibE3S3jUVoZ4Wbp5dKe/dQW4u7yKOFkYw0eQVVSc2UvVohy8yfHVco9M5/KtLtM2PnoudLzG8xu4VFPyJPwgc9E0DJRhwUlPJSv1NTJmVLNLlTudC8wuVVcmx9mdypJnyuyVQ2p03jk7BRRMibpYP4OstjJvEzdTUz4TgqGsc0aXbK3uix4T3QKLgBkqorC/wALNlDTOl/0UMDItvKuM2iPSNytKo4eTEG9V3Z4muVEdM7T01lNyn9tirfPkct3RlOwRgqZrKd+rUqu4ul7N7BRQvldhoVFaWx+KXuf4aaBkow4KrtTmeJi8cRVNdCO0inndN6dlTUn3SIdvKJwqqTnSZVDT65NR2HXdG5YCoxpeD0zRCVukrDo3fkKGXmNysolSzNjGXFVN39ok5z5Xd1R2p8vd/YKCmjgGGD+JqbfHMqm2yRJkj4j2VPdjtIoamOX0lArPkV02BywgzJwqeHlM09deMxINTDloPTVw58YVPJy3KWoZF3cVU3f2iUkz5j3KprbLMqa2xQ79z/GEA9iqi2xy7Kotkkey8cZUF1kZ6u6huUUm/ZNeD3Cz0yP0NynZcclUkP3nyKoZiK0qE/tjjlF2FPcIY/fKluR/tjCc98pVPbJZd1T2uKLuUAB2H8fjKmoYpfZT2dw9CkppI9wo6iWLYqK8PHrGVHdIXb9kydj/SVlalNJrKij1nCHbss9OeE3oPCI+AJ8zWeoqW5ws98qW8uPoClq5ZfUUyCWXYKnszj61BQRReyxj+UdG124U1sikUtnePSn0czPZeNiZXTs2KF2kxhyFxj9woblA0IXCA/chWQn7kKmP/MF+oj/AMwX6mP/ADL9XCPuTrhAPuTrrAFLeGYwAjcz9oT7hO73RdI9R0cz/ZRWZ59ShtULN0yJrPSP5otB3T6SJ+4T7TE7ZPsv+Up9nlGydbZx7L9HMPZGGUey0SrTItMi5Up9kKSc+ybbZz7JlmlO6ZZB9xTLTC3dMpImbBBoG3/sPAWgLQ1aG/C0N+FpCx/71//EAEwQAAECAgUHBwkFBwMFAQEBAQECAwARBBIhIjEQIDJBUWFxEyMwQFJygQUzQmKCkaGx0RQ0UJLBJENTYHOi4WOy8BU1g5PxwnCA0v/aAAgBAQAGPwL+RbEmLG1R5sxoxqjSEYxpRjGMYxjGlFioxjCPNxa2YtSf/wCC2AmLGzFtkXlGLRONARoiMBGHTYRhGiI0BGjF0mLpnHmzF5JH893EExamrF9UaM+MWJEYZ+MaaY84mPOJjTjSjExiYxjSjTjzgjziY00++MRn4RakRoAcIuLPjFia3CJLSU8R/Ol1sxzipcItTPjFiRGGZjFqxGlPhF1JMXUgRpARa6YtcVGJ6PGNIxpmLHDGnF4AxeTGlFixGObaB4x5uqfVg8kue4xfbI3/AM3CqiQ2mByq57hFiBGGZaRGmPCObSTFkkxa6qLSetWKMWLMXrYviUWODxiw5mEElsA7U2RNlc9yok42U/zRJCSYBdMhsEWIE98WDLjF5YiTYKosNWLzij+B2GLFmL97jFtkXVA5kiBLfBKBUVugkJrp3fzJYiSdpgFy8YklIzLyxEmk++LVy4RaZ8fwq6oxJV4RI3TFhB4Zl5Antglo1hsiSkkH+XrImRUTvgGU1bTFgy3lCJNCsY0pDdFv4hdWYk5bGl78yS0AwVMYbDFVaSD/AC1ZAK7iYmE27TFmW1QnEmhIb4mtRP4rYYxmIAVdMTBGUhaBBUxaNkVVpIP8rgkVE74nVvbTFmS0xIGsrdFhqp3RM4/jV1RgB2wxNJGWS0CCpq+nZriR/lO6mSdpgKVeVtMWZLTBSi8rdFqpJ2DPm3R1Ve0qwQlD1JZbWrBItMX6afBIi7TVe4RzVJbX3hKCVUdSk9pF6D+F2RaM6aVGAl2wxMEZZykrbFomjaP5QqtpJMBT1p2RIDKQDWVsEWqqp2DPC/NMdtWvhF1PKPdpVp/xC3AAlKR4mHKW6sqfpBtVu2Zbrih4xzykEb7DFWlLaQ72sD74U9Qn0Uqj43TeH4PORlE9U/fBHophWwmJY8YkZRdnLfnXVGUALuqizJIiCti6rZqiq4kg/wAmhTl1EAITLLaq3ZBANRO7OCEJKlKwA1wHPK1I5MnCjt2rMK8oOMqaos+ZQ4qdbfFVu4j45Gmh6Spw032UgZOcVb2RjFWiIqI2/wCYrUp1TqtgjmmWxxTOL7LRHqpkYL1GqtUjaMDuIhTLyChxOIP4GDLHCJSMJVr2ZDvy2iNH3wNufI3kwJG3ZlqrSDBU3fb+X8lVW0zgLXeXFmQqWoAQUs2J7UTUZnfnGoAllOm6rRTBZ8lprP8ApUlWJ4Re5V04rItMJKuQRyckJZrTIgXgl3WkxaoDiYYksKSnGXGLiFq+ESZa5NO2K9JXyitggJSAlI1DMCkGRiySKU3onZ/iFtuJKVoMiNn4DaZE4QmtqEW6Rxz5xM9BNJkYCXvfAINmUraur+cFDiZH+Rwp26j4wEpSBktgoavL+UVnFE56GGRNa/hvhvyVQlTQ3pq7aoFJ8rPlhKrUsptcX9ING8lsJorJxVitXjF5SlKOswJKUg++Lz6j4QhkTKLJzi42keHQBacR8YHlJgXki/LWn/HX5iUt+uFlKTUSYxmTj0NYdHTJOFKmgkp2QEPJqnUdRylK0wSLze3+RAhtJJgLdvLizJNSoISShvoEtMtla1ahH2KiDl/Kb1iyjV6sF18opPldVoTilrfxhTryytxWJMSEWY5Ly0iOWFqBFjZ98XWRFjQ90eaT+WLWkxfaPgYtWUd4RNCgobsiqOu1C9Rh5j0QZp7vXRWw3RJtITvMAqWauNkSHRbDEj0PlDuJ+cUlQsKapHvgB63fAIOSREFyjj2YkoSP8g2CSNsSSm3JbBbZvL+UVlqmehpFLA5xy43DlFohrU1zz1J1+EFSiSTryVlECJNoJO+Own3Rzjs+7CW5TRZjFjaR4ZMYxi0A8YvNIPhF0KQdxitRnp/AxUpbRUPcYS6wudW2WsRRaYnuHgc7dCuUwGzX1eZGO7pZ9D5QH+kPnFM4D5x4wHATpSlAtty1k2ObYquJkfx8OPCSezASkAZCpZAEFLRKW/n0SUpF5RkIbaTpJTUHHWYcc1E2ZZunwi4kDKhWqyPOCLFjPquJSpOwxy/k9ZCx+7P6RTGCmrSqOmakcNeaCVDHARhEzBOvqtY4DoLc49DTv6P6xTu6PnHjAPriJpNsBDvviYyFKkxbajUfxwJSJqMBx21fyizJjNeoCJrNmpOzo+WULrAn7UKQk+on9TmDJM4RJsECEzIM4Gl74ulQ+MTBmn4RLBWzPVTGhzqUlDo/iII+YzKwSSIF0p3wvlJkxWdsHZEAyw1ROUhLVAUJ2QpalGasAOpAWz458xhkq21c3f0NKG1j9Yp/9P8AWPGP/InKEqtTAIOQpWkEGCtAJa+X41UbEzFYiazicpbZtX8oKlEknpKyrrzl47oK/QFicwZPUESAkISd8J4Za7Vh2RVXpbc6kbOSVAy2LMDlNLOsldtt6jM+jnmrjlA15pEbR0D4/wBAx5Q/pGPGFd9OZYbIFtuSREF1gXdafxiqgXdZgAC3bkmo2QW2TJPa6VdMf8xR9R1qhTaVFNGQbEjXmyiqNcS9+QxwzKydP5xybvvzaWvY0RmJNYW/CALfHNnqgtt2g4q6hNWhASMBnyGkfhkrqFuroFSwz1f0VRT/AOiqDDneT882skwEnSySMF1hPFP4tsRrMBKRkKlmQggWNfPpjV0niZeJhQnORzp5SIUM2Y0/nHJr8Mzk9bqvgMywkcIkq1J26oukEbomoyEel7ouJCeMVVKs6gmV4K2RybSCLMTZKJDOqolxyVlT6Df0Cd7S4p39FUGHuKfnnVk4wEOaWS2C6wJL174kcfxOsbGvnASgAAZCpRiajd1J6dhs4oT8ZZ56KY0vnHJq0tWUy0W7ozrCRwi09RkBMwJ1cJ7YAGAzykTrZRus6CfQNf01/KKb/RVBikez888EG2A24b2UvMi/84IIkR+IhShJr5wEpEgMkzpahBW4fDZ09GaldnWVwEIYGVKRiTKDItq9qAFjHOBgZ3Ko0hjE/TTjBl5xdiesTtq6yLYFqVS2CJDDoE8MpTtz7vQsd1fyilj/AElfKFeEUvgPn0ExDKiSplaUmt2ZjDKXWhzg+MSNhH4eFKHNj4wEpEhk2uahBW4ZnqD9MUPUH6wq3RysD1hCzvMGjqNuLZOo7IKVCShnSzpwHhdoxE1fSCtXgNg6xM1RWgkkS6E5fDN2xYIE1CcWKAPQUTfW+UUkf6SvlCvCKb3R8+gMUesJhTKMe6IbTW5t2dWerKXmRf8AnEjj+Ggkc2PjASkZJC1ZwEFazMnqAAx1Q2x6VWR464cc7SsrW6ZieTnrHP4g/WKldNuioYGClQIUNWZPO5R0hIEdlsaKepqMxJImc5M0Nk7hFZKUzFlnQgqn4R6u2FSldiYxi0z4xPAb4u2xactvQ0HiflD39NXyhUU7+n0NC/oN/wC2KCddZX6QG3DblLzQvaxt/DLfNjGAlIlkkLVnAQVrMyeohahzbF88dUPyOgmqOJzE90/LNDdJnIYODFMV25PN9pGZVOTCJqsgpZk45uw98VnVTPy6oqWsSzi3WqpUNlvhEkiQyjNmcI0x4RI+lhm7BFtvGNGJy6Kgd/8ASHe6flC4p/8ASPQ0H+gj5RQ/6ivlFkBCzbkkYL7Qs9IfhQSnR1mAlIyetqgrWbT1I0hYvu859IZQTa4qscxvxhQ2E5s21lPCJUmjoc9YWGNJ1vjFlMEGVMTX1RJ11D2yqkziTDAG9UX3DV7IsHUgSJTwgqQkkDZHmle6OTlVQiypsz0Mpup0Z5jJnhjwgEYGJwcs1gSiqgXR8cxMl1pwCOl8n/1P0hY3GHIp39E9D5P/AKCYon9U/LJMQEKNuSRjlWxzZ+H4QG0QEgW5CTjBWv8A+dSZYH7xQEMURHpECQhCNSEZjHGHRvn10KGIMEenv1QEpFggttiRGJMTOOfyZkFfPLVTofPIg7skhlUJmQOvMFbCKqAOMVQYSTj0nk/+qIUOMO8T84po/wBFUDoPJ/8AREUb+r/+csxAB0shSoTnH+mcPwYISJkwJi+cTkKlGCpWGrqanT+5R8TDDfoomYe3SzKP34B7SAevJfVO6mqN5ySCEwVLaTLcOgCQqzhF42bBkBIMjChrlmqO+CVaIg2S3ZOEXTbvyJ6Sgf1kwobzD3j84pv9FUDoPJ/9EfMxR/63/wCcwKEDbkUkiC2rw/BJCOVcF8/DISY/0xh1Snv65y+EPLxCAqe6HjtlmM7rYo/9EfM9dE5y3RzTVRraTichJwGyPUGHQmUgE4k6ompSVhOyEp1pEBQ1QFDA5SYG3GFgYq+GWe3JMptzsRHNsPK7rZMXaBSPySj7oR3lpH6xahlPF5MY0b/3iP3H/vTFEedbRybboUSHUmQ98FQIqzxh0rYdTji2dsU0f6KoTw6Cgf0/1MM/1v0OaFatcAg5CRpjAwUKElD8DD7gs9GJCJwW06OvqtN/qR5QpxHO0xRCO6Ibd7SbeOY67sFWEp7LSR1wmqqQxshVUCsbJnVCGrLqQO6TiclrjgGwWR6pwPQ2lQrHVFgA4QpXKALlOqcijOzZkvECLqhaYJzEcMwcjRHSNpEh8YnS6VRqOONYx+0U1947ECqI5ryclw7XDWj9mojLXdQBFjtXhF6kri11UecVGmr3xpq98ecVHnVRY+uCOWJBsM4tozXhZFqVJ+MWLHjnUHun/cYa/q/oc6jPpwKBXH6wMnLti8Md/wCBAkc2PjAAycm349Wp7G1aYb8msAAITI7hmtDW4qKSrVXkPCzrMgCTuiSrDvivVNfaf0i0e+CZCaYLrihdOBi8oDJ7Q6FImJj4RP8A4YUtxMIOJghNoVZ45DPHOUNQgFtgpb7bl0R+200ur/hsx+yUJpuXprtMEJcVL3CMVeETqHxi+6hPjF6kjwEabiuEebcPFUWMH88eYH5o8yPzR5r+6JijOASnbElJUnjGMYjMsJi22N+Wie1/uMJ/qj5HIkqsrasyif0xA/hKNm45JGC6gc2r4fgCW064SlIyVU6RifVqS0fSRW90UpSu1mtBImUImBtMoM8dc+s1k2Vd3wi0A8cpbbvVsTsgkkJAhNaXjqiYtG6FIOBga0nA9Am2UWAkz1aoKVJt2wZi7KyeqCrXujGDZbksBMWyEFJjlVpDNG/iO/oI5lv7TSR+8XEiuqjspsySfWn3xzTFY7TF2ogbhF5xZ8YMBLDFGauidZN6cUakN1eSXrTvtGbIRR2XrXm2ktkjXKGqWqpSGHG7K6cDrsikJU2mrOwbIcNVaVAE2KiYmI0otEfXLJWRhlx9CXE1pg8YqtPIWuuDIRMupW7tGCeECQsGZRO5CiMQoQAdIZFIUJzhTasNR68AMYrK0zjk3xNWker0dxRuE1FcDCngNE1V/pmAxRXWrJEqlAUFBulanO1uVCmXk1Vp6wA3aVejqi9Ke7KTvgqCTVGJ2QlPaMokBIDIfVPQJJAInkJ5SQ3xJNYzwi+bN0CqkyGKougmLxkN0SGEJZaQVuKwAhD9OCaRS/RRqTEiZI7IistQAgUegsl90+AhAWsKUrSaZ9HNorRwW6lPxg7h+sUqi4vUXR4Yj9c2jpOgg8orgLYc3RTaMT5tVYbqw+sOA4w53TAyWASSkqVPZDSWqsqiU1dkLRaJGLeiovA/OHvD5wFCAck0i+MIIOI66H1i6MIlE4+0LFh0B+sYRYI0T1Zl5YmVJ5J3vCHGj6JzJRXTan0k7YQ06q9LmntY3GFsPJktPx6GUjPhCaSHOTpKrQ04JXYSKWlLVGBvrC9W6B9jUhdCcNiyq1A37YpDSWnHGmhX5SVlXb0NUSnvidYhw4yjEnjkKterJUCSSRNSjq4RgMq62BsiZSqW8ZkhjGiBxMK5WsFA6oJ0kysnFmSWrKaolPZkS02kqWsyA2xUEnKc4Ly/+aoU44q3WTBRRwFq7WoRN1ZUd8UeSiJBXjZBafbbSxX5NZ2HtQS2mVGpF9G7aMygA4cpW9wnD26UFk6NJQU+ItEUlgaIVNPA5nlCmKwSkND5mHHD6apw8xqfZUPEWiH0aq8/fC+BgZFvqxfP9qf8xRVHFTohXAZK0jV6FtJN0z8IpPd/XIEnRySjl0CzX1wIGGuEpAyVP3KLV790WDJOCALYJ6quiE2Piae8IRSANG6rOqqPMqx3b4qWfbGhNtXaGyCCJEbc6jN0uiuoo6zeVqlDi+dAa0m0qMorMtIUoyFYC2UTmlaG1EoIshxsvqk5jwgI5dVSrUq6pR9hp6gtt24J2eEUmjAzDS5Dh0AUDAKnLdyYuIKuNkWKqjdA5RU5RdkBtMFU6y8y0DxyLq6M7MrRbAnVtHRveU3R6rf6wp5yczs+UVZKbZ+eWjesFj4RTkntz98FnSp1Dw3yw94zGT2UrP8AbFLPrShp9Ok0oLHhFFpzXm3Uys94zG9TlIBX+bD4ZKE7q5UD32Rx/SFeORtpu1bhqiG6I3oNioOA/wAxQhteTDuRmuRNys4meof8HQt+Pzik/wBPLyZNoicKSROFIOGrrUhjAJF845Ettibi7BCW044k7TlPV6K72XBFJo6t8oW2rSSZZwZKucatTvEN05oXH9LcrMSFGqkkAnZANLpvKACsG1Xa3GFssiQThVtBEKXyipnfmBdRC5eivCEppPkyjAD0m0Quk0OkcnSl2nfxEcnSW5T0VjRV0hqKKZ7IFdtamu1K2LiTV7RsyzMFDRs7W3LyqsBowFlIrDX0fk9hPpICj84VSnFCZmJ9mPtfkuZeYnZrO0ZaAr/VA99kFX8VpKv0hFY8y/za/wBDHKtjmKTeG5WsZXT2aOv9IpZ/1DkLeLtFmn3Wj4ZW2k6TigkeMUehI0W/0syBQxBnFFe1OAH3iDkeppwo4kjvmHF6sBwihblVvdDvhCUC1xZqhMNUVJutgI8E2fODmnKjxikj/TOVAaBUs6hEjjgeOQlIvC0RLrPKqF0YRKJmC+rE2J3CBm2xZ1Vh7+K2DBfAk6g1HR8jnIdRpJMO0bU+iu3uVBBFoxySAJO6G/KPlCRWoTaZq4GFIqIkCRtEugDyEzeGEzYILPlGgtP0ZWNWyGaXQ1lygUnQJxSdh6MOPJ4JissyEGSpH1oqhYrbBHnB4xPlEyiq0LDiZYxNKDLbHOq8BBqky1dJQqR2WSnxEU5T/wB2cmgyxwtMNuKWHfJlKucujR3E7ILjY/Z6ReTuOsZGnR6Cwr4xQKQPSCkfrkUyq2m0bX6wwPiItxyUw7GJfGKSdrislJoisH0Vh3h/iHmdSVWcMjTh0aOC8fDCHTPRu5fJbwxDaZ+FkGDxhqjYPKFZfeV9BF0ExWrBRbaWqzVZD8u1KcKpRF2jJre1qimUo4Dm0dCOJh3uHJybQ4qOCRvio1aTpLOKv8QHBoq0vrkIjlEi6rHrCWxrhKUjIGtWuBEjGOdPqjG1lRbhwymhwTI2iDIzGrO5OdrZrDhrhak+bfHKJ/XIKWpS2WGTWCx6W6HWZkyMgkHoaqJT4ygyoa3GZXkETSoRSmKO2VLRSKyW52gQUqBSoajj0NZISTvETUswEqVMDOaHq9M95PWbVX2v1EU6hOYtufMQ/RXRWbSooUk2g+EFtJCSnRONRQgqLPKtj0mTWgpNh2GKFSRaW+TUfdI5EVzJh/m17thjl2xzFJvcFa8nlJexCB8Yd7xyMUkYtLCvrDNJRoOCrP4jJ5QphwMmx4WmFLOKjPK1/puLT+sXliKzsiwxN1e/YIUFCuUm3ZOJTu7BFOd2NhPvMLV2iTDbSR+00i8eJ0R7oboiTYjHec45faMOd0xJN1pOm4dX+YDLKarY95O05CDgY5JR0fiMikkYiFNqxT1fllC8rIVGCTicuOUZZZMIwjDqFNo+uQdT4Ywy8NV057S9U5HhDK2U13qMSCBiUwQcRth2lpUlTtIOo6O6HFIUVTxO09EOTXXZ1tLwhflfyQ6qj01q11tJlOOT8rUVikITi8bq4ec8j0pRcaTXLDmzd0gQ5OStkGolXJ6oCZkDbKJyKj63TzhtT5qlwcmpepWycGlMisl4TKd4h1pZKUOotnqIj/qFFJ5PB0D/AHRKnUJp7eUyMO0Zis3RRNO0p1x+z+V2/wDyolHNUihu8HJR9kpiUimti6qtO0YGL1CpH5I8pqcbWgkpsUmWqCeTX+Ux5tf5Y82v8sNgg8tR5t/ltHwjQMMMYOPJrHir/EWutiLXZ8BGCjxinsy0VJV7xKCknCyDSFDnaTf9kaI98FRxOMBCElSjqEU146SlH4D/ADA5Ufs7I5RzgNXjC31YMf7z9IWoYYDofaMVlzSzhPWeEJaaSENpwSMtXSe7OzjHKqUSdcDJy6Rhj1ZKZXRaYAGTkxgnPlGOZbGETAg9Ow6dCdVXAxSGNeroBVM1qR/cIBcQEPnXgoeMCjUFCnUNpM5aRJ1xIggjbmE68+czyTgqODamDyTgW2cIpHlQlLq3kck1yZmEzxJ6EhblX9Y0lwOQHEEwgyBAM7D1SbdU0tsSt7Y+sVFt8m8tJSUn0opPkumBBmDUAwUjZC2mjWaN5E9kU6iPCVoPvEONkrm2op90WOKhoPuTYduKnq2GEPsuLDD+/BWuPKNIdcUVJKqszhJMfeVx59UefX74fojqir7QmaZ9pP8AiBQklUnVirwMM0Zs3W/kLBmU1rtsT9xg0NPpOmZ2J1mEUZkc2jUNmoQFPTQnsjEw4ijpSKuMoozOtxIJ9ozisoc7SOdUN3oj9YDRPOm1XfOOWcs8LfBqkzSjtf4zC0wQp7WrUj/MEqJJOJOuJJwjk1G1MTggiFtnqoUoX1WnITr1RM49BjnGD1BCyedbuL/Qwuy4u8nPbcmaox4Q4phZCF30y2GEs0u0bYLyJIpH8VOPjHI0hHdUMFdIE0Roqo6lc4mpWSvjFKao3mkmwDVtHQViitATale/qwSs8w/dVuOow15SYHNrXynBeseMUHyvRFFJEr6dhwgOtkJpzXwXs4GBR3ZDlUqSbMCIpVDecUy84qs0fRVMQuj0mjydTjZFrUuELo5M6Q1dme0MDFOKrpKlg+8CLFqjSVGKoapDZNZpQUIo3lCqrl6pLY2A7eEPzSbt0TjCNUTtnDKQkqCkqSqWoSimUoz5d1NVR1JSMffCqQ5prt4QWmlSTrI1wxRh5x0icNUY/dqPecl2UiUvGCJDkqPzi+PoiFW2I+evMJzQ9Sk38UtHVvP0i3KWaITV9J0a+EbokNHIlwaoEsnLJFqeqAkXEQBkqjRT1LCMOlrfu1WLG6JtyK03kEa+gbBxRZ4ZEzUeT+UFl5KVTE7NW8Qqju2jFC+0OjC6M+tvdOw+EeT/ACoWg1Sn1FC6uC5a+ilapJ1dWdoNJP7Q2JVvkqKd5FpQk9R5o8DgYKHfNLuODZvii+WqOOb5VJeA1b4oVLbMioVJ7xaIo/lZsc+0KrwHx/5vyMtp83SLihDlHVY2tJwgZW2m0lS13QBrMMJKkcolASpIOBgvKHNuaJyAZKdS1Cq043ybajrO6HAROtIQplv2jCa3m03lQwsiaUqK/AQ4sefpB9//AMgLV55/nDP+2Dl8YVwyhKQSo4AQHXJKf1bEf5yla1BKE4qOqC01NFG+K+MTMbszkirhE4IMKR6OI6mmekbTkMtJVg6a3NtEFSemFHcNw6J2QaUyLPTSPn0SRWPJz/LCqkvtLd5G4/QwQRIjHokOOspebGKDrij07yaeYoaaq2NaOjrjx3dVbpLWKcU9oaxFG8r0S8CmquXpt/UQKU35qkXx3tcOeSqXalxPNT9JPZhIbCnFUUhW8jXC6C+f2el3bdStULYOhinhFERRjJ8uCqdm+KPRKQupWHnNQ4wul0NH7ObVtj93vG6AsYyiUL8r0iU7UsBXxVBeBISk3PrC23gOWR5wDXsWILK/A7RA4wp99Rb8ntG8vtnYIFFowCCkVQgfuk/WG02cqmu1P5Qa2lrif72kf7YdpBspVIEu4j6mOSBPINAcpuAxipqRqGrM8YWd2QNtprKMdp04q+mXlXjJOraYmq60NFA1ZJnLZEhCVDVAycokXkW9SBIupiWQgaKenxyzyGD0wYfO4KPyMF5ocycR2eiQ0o3fmNkGhU1KQzSwHKPSUCSmydR2icVXhWaOi6MD0TbI81SAW1p3Sgywn0SJGW3f1b7O+r9ldOPYO2HGkjnU32+MIUklLrR8RFGfrBRWgVuOuJqFajqNxxPyhNKH32ii9vlj8IpNNIsZTVTxMUl70Z1U8BCaHTiVUbBDmtv/ABDtJoABSsVi0nDimA0q6yi86rYmBQ6OKrKQEyGpOoZEPsmS0/HdCXWpA6tratkEU+TLCBXVJUyrcN8JolFSlCmxJKBgz/mJmc4NGnatFYD1hCm02Jd5zw1wmnUsAIQnmWl6h2lfSKc4J8mlxIBOJxth2lqHO0g8p4eiPfBUozUbTBy+1C+BgIQOJOAiq3idJR15ZqtWdFG2OUePAahG+JnLuiScMvJk8IBgiFo1YjqKZ6RtOQy0jYOpyMYxIHqHKaamyULSdYgLbto68Ds3dDMYxQ62k0Cgwy5SWxSWAOSpDStY7XGEUzyevl6A7anand0AQ2lS1HUkTh5ThH/UnkVEtg+ZBxJ37ujc3Js4wD094gRMYZyfJ1JNv7lZ/wBsO06jDml+eQNXrRTkAyUF4juw95P8opSpxN01v3g+sUkVq/k50TmcU8YeclVddBV4qsHwyteTHSVMuHmyMWz9IpTTASnlACaqdIy1wSomsTbldfqEsstmZ1VtUO48oArDGcW468lEU2Ky+UFm2K6m0KcQCkTwAnCmGiS1WtOtwxyTmKzXXL5QijDBu1Utuoe7Icp70Ve1ZAQgSA+OXk25Lf2ak8YLjhKlq1mJDHMrK0fnGwbMxKhqgZOVSLU9QE9FNsAZCkaKOp4xjGPUKXRicQFiHG1CtR3dJBi6Zp1dFSGjgokfCFUakTVQnTfHZPaELZKw1SfRUPNuDVFR1JSrMcpSKtRtYQaxlKeuOcWumOdlu4378TFVmrRmpSqMCrZxxOYEhJt2xNKqytkWpI450+0enmkmzHJPsxVULSJ2ZqHGzJaDWB3w27drKuqQdR1iPKbbaT9leSHG92MxFJ8nvtoSpyaUOiwnZC/JlNFZ9sGTm2W2C0zXWlIDqkDVlc8rv2VZpZnt1mDSH2eWY0Z+l3oFMoLiK69FY0XNx2GFtOpKXEYpMBlqxItW4cECEUGgYgWf/wDZjkqQ4VMuHFXonbBpTYt/egfOAAJk4SgUunS+1JvVSbrP1MPUxtVjjddJTs1w7S3BNqiprcVaoo/K2KS0FubibYKlWqVaZ5qu/mFmiG3W6P0iZxiScya/BMW53Jk4ROFAwts6j04UReXbkWrXqiZx/BWFE3VXFeMcskXsfHIHCJJOHQqVtVCuMfZ1edRa0f8A8x9mpt5vUvWmJ6bKsFjK6FiS3ZLPvzRMTnZmSWkERdcIG8RdcBO+KxRZugJSLTASMB0cq3iImDZlMvHKDsiYwzQVTNHXY4kbNvEQkpUFAislQ9IbYpXJzFJbXyiRtTjZDL6ZpTyfLmXCKXRbOXQke1tEGl+TgkOm0tjBfDfCKEApKp85P0BrhvyXRLraU1bNQyGrfYXptHA/5g00uENpEkrGnPsmORo7KhPSJxdVBpDqqylm3IKIRXeQLJ+kiHX6wLwNjy8Gx6u+KiZpo6TYNat5hLKsUtqQfETijBQurNcjtq1COSna6oz4DL4ZXe/kKlqCUpxJ1QWmZpY17VZJJwzLbXNmyJnHPSqBkDwG49MhGrXAGQNDBGP4OlwedSLe8IFF0WTfJ2Jjk0CSGxYBq6G3FKPicgIxivrVjxj7PSL1HVt1RyrN+jq19mC/SPMMX1DbuhhTh5ymPV/Ceam0jXZ01pt2CMZHfFhB4ZLxt2CClANu3JNJlCVbckjgYmLwy1VYbc40StzlGVWTPsHGKyCEUtq1pzYdnCKY86yWnm6rSkn0ZYw7S2llLnKlaVCGvK9GRylHXZSqOPRV2hH2hlCU0l6SSqreIikF6tXra8iGWklbizJKRrj7I85WWTyjitSVbBCgikFDqFYEYcd0KodLRVVqngYS20lSi5oga4eepawaQtuquWi2Nm8wrkVJVWFdEtcoZa7ahHlG6Q0HRVVFHaEppBWr1UgQW5+bEvHE5fA5X++PlHKvKkn4mLbrQ0UCJmN2WqkTMSbM3Na9nDogkm1ORadogpOI6UvEaWGRazqEKWcVH8HboUueS2HF+MU1+yboCEHccYeVtV0AJ0EWmEMDvHNDblrB22yhVJoVtDWqa0/wzq8IoFCoq0LeobIUpj0yCMRtiWvMWrYOlMsZZbpixAnBJxyyOMKHZOYFjXjlxPhElWjbExlYfThWqq3pOMN0pQmxWCHdw2xSFMlM3EWKGuyzI/5PcSVsUlJmNkUZxpAdSxVrBRwEcsk1XQJFQ02jv2iOTpCbDouDRVH/AFOkgfanhzST6IgyVNpJs9Y7YS8yZKHuI2QmqmatnpMq+kKeU4KRS1GrygwREgeZThv3w5RScOcR+sK5LzZbLoJ9GcKo6JVVJuA7sTHlN/tVEjxtMPOE6SycvgfllpFYFTilCqnbHKOqmdWwZplYkYqOAjk2tHWrWro07FWZCIrgXV9IlIxJlCEjUMiWB6WP4PRWsQVzPAR5TbOHJ1U+zCPE/DoAhImoxbqtUdphTisTnci4ZsqskrDgd0DypQVuOJsmEm+x/iFvPM8l5RZNUPVZctxgpUClSbCDqyz1qt6UnZnoUbTjCoEjhFht2ZSk64kccywwBgcqm3LXS2W194a48l/alVbqEzOowfKNFRzSjzyR6J2xSfKb1gVNIOxIxikmlW0almR9TZChRCUutG1Xoy3wqkUmjN4y5M3kkjXDRdNdJVIiVkoFIYvUddvCAywiZ1nUILVflH1pqvOjUIcS06eQcBTWhTLokofGKMij6dbXqGuHJrBbaNpGKhDRtqKCkCeoShTkr1VTit5wGYngflkE8m+JnLyjhqMj0tvCAhAqMjBP16ScJORRGKb3SVyLEZJw4vVgPwcr/htKMM0pZsU6a53GE91WeABMmKyvOn4RyTZ5tHxPQJLZtTh9IoyHm2UslYCyBbKFUiigCnJE6v8AFH1ggggjbqiZ82MYlq6ORVbui4kz3xNRnnj1bIUrJMYx62vLbFkiIBMvDMqnJTKP2gHB8oQ2BYXBC/J1K5xbabCq2sjYYRRKIA3R5hKgMZQijsialfAbYLbJmrCtt3xKkOK5alHlSg+gPqYXRHkoDxtqLwcH1hSeWT/09X7tdqk7hB8neTEgK9JePvO2A3WN60xWTeQrSQcDAcSCRKxets7IUhQqqUOefVr3CJ1aqHqzctkMVMWCVLOwDGKaVD0ao8TmN+Pyy78wOv4HRb1q/wARNWAwAwHTFs5DDiNhs6MKOKrcij6RsH4RSP6JhXGGa3n6NzS94lYYI2ZoQgTJiZtd7UFhs3zpHZ0dF8oUc/ttEFbvSxHiIZ8qeTaiXXBziDZM/WAh5haAPS1e/oyhNqte7oTIGyLxKEwUnHLMG2LwM90XTlmDbvi0ZTwyUORlWJSd4lBZV6STye5Qh9webQ1VX4mz5RTaG8CoosSk69pG+HGmq5dpJ86dY2QukUoTo9Fvy2nUIXVNZM5CWuD5R8r4IFYMfX6QyhSnUVsSE+bj7UzV5YjSTouj6w4d8omgcnR9bytEfWG6O3SeZM5gpmpZ1wmqrnE2tkn4Q9Q1gpcSa6QdusRS3Rp0x0y3I/8AsU1JM6tUpI15jXj8slmWQxiboC6Tqb1I4790FSySo6z06VQMiHRwPRIb7RgDIloYJtP4Q2Dg4CiKSzIhFasngYDiPaTtEOKbNxRmMyei32jFVscTtiSfOqw3QScejcoCzNp+1M9seUPIbqylikGbRHonVKF+TfK8m6Q2anK+ivjsjlKPJlw2gp0T4Ryb6KuxQwPQVE6XyzBMETivI1dubV1KyJ2yzQdkGtdy24b4uWGJERxyM0hOLSguJpsVY4mcU7ygkVS/WdH/AOYS+0st8iaxc2Q+w4lKqpktB/3CDQqKoqW8u85rMfa6SmvTSJy/h7uMKRSZpa/dpGjLbkDLyS9RVWFOscIbpLgrKnWqgVaw2KhdGbaIbbu8mhMh4xXcVM6t0Lo6ja1aO7DPlBKE3UlKljFU4Lia1Zzmm5nAa4QBg4W/9uYy4rRBtjAqaValYwOUNtpKlnUIqsqC6VrdGCO79epJGzI4Ncpjoi5qTkJhxw+kfwhl0YoUDAdYH7Uzo+uNkEEEEbcs0okntKsjlH1hUttiY5pMmh6Z/SNqzgmCtZmo9Ih1BkpBmI8n+UmDJa2gfaBig+VmpSpKKq+9CWlzdofYOKeEAgpdo7n/AD3xVN5pWgvb/nPC5WSyJWUySrCeuKxQFcYCUie86s5EjkOeEq1RMYQE+MSBitryWwYoSp+ebTL3RQ/JzCLXl6I3Q35HbUOVNr6xrJ1RSvKdKuJCSlE9msw6660VOMqBCFat8NuBakUUebUkWE74FF8sMpRPB5OAP6QHW1ctQ16Kxq4wKfTQjlyJoQr92Npi+1ylEnfOs7xHLUN1Cj6C9vqqhxtCeQpjek2YCEBSaTOqANcCjvUgF8KrK2VuyIaVMllWrsr/AMwkW8oyy258Lc2q0+4hPZnZF4tL77STF6iURX/jl8oUhppthK9Lk9fU6u2AYMOo39Ck61W5FyxVZ+FUSkuzloLPwnApEqq1fvG9cEOLdclvlFcMttga1RVo6a52mOWpyidjcaq3opgrWZqPTIaPoKJEUqjK0mFV05JEq+zOaaRq3jfDtFdI5VMjWHwWIW04JLQZHNKibBEhdT88iZ+jZ0Fp1QAZ2/CFKGHQKT4xWBkYkccthtjfHktR/dthXwhVMYZSukMoK2gdRhtoEqefXar5mKL5Ios1OuAJqjEjVFG8ltPBmlFq6UWTVC/JPlVCRStGRwc4b4/Y0GksKN1QNo4wzRaUQ7yYrKnhPH3QoNq5qdp7X+II17NkBQvNnSRqWIYp1Ge5N6XNv/8A5VDVJpbKftjcwgi0J3xR3UJV9lbvJdSZ3t8MlWhSLFeosRSwPSbUke7raVbMqXANLHoEI2mUJGRDQ9G38KozKtFbVvjD/KGxF0J2mJAzfXh9YAFZxe/VFZV57bsiqJKe7P1ialTWrWYShOrE7T07zWpaIlsyMKJmhNnsnERR6e15t4VSR8MySlWwgJMwOqggTgTtgnblBicooR1cgn5R5TbVWKOV5RB2T1RS6VR2+devcNw3RS/LFIvEKqtT7X+BDjrSjUbuoPDXHKpUEeU6OL4HpetARSVcuvarUnjDjDTaW2lWKVOZUMgUk2wQRe1gQqqsuFVq0HRJgootH5BSsVV60uEfZKdzlEVZM21fqIcND8y47WElT1at0TUlP2aySvWNhEOsnFtRT1rCKpxFmRShim3oAezAgmHV7/wqhnYmr7oUcSLqU7TH2l6bbKra6sTwEciykF3WTq3mFJo6prOLn0gkm3qKe6Yd7xyAAEk7IpFAcQpNNo6boWm3amLZcCItR7o5udf5RM49SlV98SUIsBjRlxifaiWpOa2yjScUEjxgUWip51fNNgaobo7TiUhu86sjTMN0+gvr5AgOLbGzbDVBYWpKJXyNeQeUXJhtHmh2zt4RSmaO8Fqa06mveIKF+B25TTnBJlkyTP01/wCIqWckLOUOsxyqBzJ/tgAYmKPR2SA+JCe8WmA9RxJxcuUbHoLilKSZitq6yBFoizBYnkUk6xC07DLPU5tORxW78LdSkVltLMhH23yipLrotSg6CPrBCF1RtGJ4QRot9kdTU5qQgmFK2meUJJ+0MdheI4GNAJpWv0XE/WC6Oeov8QejxHUwdkSBt35QB6UVZ2Z1EfXoNuAnhHLcqhUxdKTMnhFd0yQNFAwEUjya9byd5KTrQcR/zbDtGUDIGaD2k6o5Z4FNCbN49s9kQfJdAITIVXFI9EdkQh5lRQ4i0EQ4oJCKQi1xoej6yd0FtwcDtjk0XW02uOHBAhug0QVUBNVI1gazxOT7LSpFhV0FWrcd0IpFashF5DJtJVq8IVI1kNXZ7TrMGqpSZ7D1bCMIwyCcCUNudhXzyq9a3Pb4TyJb7R/Cgywmsr4DeYM1TUrSX2jBZatV2dQ4wVuqKlb+oqOWkPek6aic1LjaihacFDVAoVPqikmxKjg5/mFU2hp/ZvTbH7vfw6pJV4RMQDr6JmlotqG1O0axDa3qjyU2pNeqpO4x9j8m1UCVWbeCBu35UUijqquJ+O6BW5Ntz0mHdR3GPs1H5NWuo1rO1RhTrhmtWOXkU0hwN7AesAmJVYKkiDE4kow4meIgbxkbd2WZyEbTKAMgT2R+EpYaFpxPZG2KiBe+KztMKQ0r2h+nUzxyMMgXin4mGaGnBlNvHOnH2CmkGkgSBP71P1ha6Myt2hm1NW0p3GJa9/U7DKBW6K2LOvYRhGEDKZwZZMcktmRzcJ5yN1sCDDyvW/CQ64OfevK/QQpCCSkm0j0v8RNuiOBPaXdEc7SGW9wvR+0U8+AESTy7ytpVIRZRx74+7ojzQ/NGBT7UXaRLiRH3pvxi2kt+FsaSlcBKLqQPjFuc361sNt7TbFKpavNsYQtxWKjPPS42opcSZhQ1QKahCFvUcgUhvCzaIApSG0LOp5H6xXobq2t6FV0wVNgUloa2sfdBBBBG3V1Cuk8fwoExKrFZOXGCEwcxQyKG6FIOIMs1asjitg/CWW0tqUmsK8tQiTzi+QHoIsnE2aO00O1K33xJsFw7dUFKn/YajmmfFZjzlXuiLz7nvi1xXvjExieinASMTCWxghIEOvbLog0TlKrq753xUcHA7egJFqFiotPaEborUd9xo+qYCac0H09tNio5ZhYFIH7xNi08RHJUhNh0XBoq69h1cSETlFoyAZDPLjGOamBkd325tbachHas/CAymxAtWrYIDTKA22n/AJbFVq+rbqia1ExUK1pHq64sdcEXHx7QjQrj1TEloKTv6SeSepFsOK3witZZWVBfBIM7u6Cy8Byo/wCTESNqDoq6NLrDim3E4KTH/TvKqEh5WCsAvhsMVVTXR1+bd27uP4pMiMIrDJjkNvQpO+BkQvtDNb4ZGkb5/ggAFp2QDyPJp2uWRz1NQnupi3yl/aIcQh9DrizMqnKOaabeR2EOQQ6wWOKI+8Kj7wuNMHiI5xlJ4WRerNnfFikOCLk21bsInKujanoZmLMi3jiROG0atJUJYTpO47k5ApJKVDAiOQpQSFn3KgqoxmOyYqrSUnfkt6H/AKZ5WAWF2JcVr/zviRNejq825t3Hf+JyMTmIkDlx6NORC+ycwCEjdkCeyPwMOHmqP2zr4QeTSlToxWcffBDGHujz0o8+qPPT4iLyUK+EVXuUSPzCK6Aie1uyCWVcoNmuKqgQcthjSrp2KiSxyavhFdMkq7SYkscDqztuVDYxUYbYT6WrcIVSHrCoVjuELdVir4DMlWro7Koq0hFXvWiK1Hcq8LRHJvJrt6nEW1YqLHA7eh+weUbzSrAs/r9YUWxyzQ1jEeH4BZHN0Z5XBBi7QHvGyLzbTfeci2lUce+Obco7ntSib9EcCe0LR0mMSrRaemAyO7rcxob4GR0+H4EKd5Qu0YWpQfSj7PRhL1Rq4xWcUVHMsQr3R5tfui1KvdE0kg7ok5J1O/GJEXthxgqbNdHxzZtqIio+kCfuiu1eb+XQOOkaNkKKvMUeziY+ytm6nTlrOzPm2tSTui/Jwb4w+qTFVWGo7ehCZzCdHdujnWwlzbomJsPBXqrsMSWkpO/qt1tauCY5ug0hXsR90q99QEc47R2/Gcc7Tx7Dcc5SKQv3CPu6195cXaAx4icc2w0nuoGep2jJSxS92ivjC2nUlLiDJSTq60RvyOjdmN7rYEGFL7Rn+Aml0qyhs7fTOyOTbsIwT2P8xrKj8YBDHJI7TtkTfpBV3BIe+LQHFDUL0SborTY2qi19scDH3hHviXLtHxjRZV7o8z7omlS0njCQQpXrQVIur+cSIkc2w2bIrN2HWnO9b5QlI05TPGA22efc1/M9EFtm35xVIva06xwi5fT8Yt6GxRlElAEb4smg7osIUN3UAptrk2f4jlggGlOuPr2C6mOaoTA9mcXUhPDqDFPQJFfNub9nWlDIYcTsVlUdgyKP4ClsGW1WwbYaoXk5ha6okhAH9xjlfKNIqztKG7T74HJtNtntKtUY5lFvaVHOOE7oP7U8kbospKifXETq10+rEjjluuKHAx5ytxEc434pi4oT2RbYoRI509fzzKxx1QmtopvKhTitFImYU6vE/DowRjui+K2+LUjx6bm2XFd1MXKA/LemUfdgjvrAjnH6Oj4xzlP/ACtxZT3J72xBLFLac3KFWKtKYUjYrUfHNS00grcWZBI1wl+nhLtI1I9FH16or+oj59aEDI8N+V1XDI4dxiWUCAuJRWlGHWOYZUU9s2J98ftDldarTUwiqKqfVTEmhUG3XBUozO0xKtyitiI5tgDvGMUDgmNNP5YvoQeEc83VVtis0oKGdt6SspxCVObTqhLDSwoG1Uup3W1q4Jjm6DSFexH3Mp76gIvLo7fFU452no9lEc5S3l8ABFqHnO85FlAa8bY5uisJ4NiJDDPU082lxtWKVQC3M0V3QOzdlAAJJwEB55INNcFvqDZ1WjUadrrlbwHWmoGRe/Ks7Tkc4ZgMBuBOBKWbonqQUU8g12nPpAUtP2hztOYe6KqTXI9FESTzad0TJ98FLXOubsBHOOGr2RYM+wxfx251kW9VsSo8BHN0N9XBEWUFwd6yLwZRxXHO0xkd0Ew9RUu8ryVhVKVuSi0aXnFgHhrjm6Ewn2BF0BPDqNJMrzPOp8MqvKLybjJqtb1bfDPtIjnKWwnisRbT2j3bYuuOud1uOaojyu8QI5ugtjvLnFxNHb4InH35Se6kCGa1JW8hSgFIXbPPLQ0aOkI8cT1prjAyJVtGVR35FZ04AJjHLIxKrbFnTBDaVKWfRSJwF0tQo6OyLVRWShKT/EXaqJMprHacIvrMtmrJfNZepAxghRqt9gfgtHccYaU67NZKkAmLqEp4DMKlYATMPvnF1ZVkcfOFHb+J/wCHqflEn+Cr5ZJDGKNRQNBNvHXmrdcUEtoFYk6oU3RFKo9G1S0leMc46tXeVPoKIgi6k8orwzio4ATh98/vVlXWkcYGRo7so45Bx6OZjGLDlHQhKElSjgBrgLpquST/AA06X+IqtpQ38VGJMpq7zFZaio78hUogAazBRRMP4n0gkkknb1S40tXBMBTrLjaVYFSZZUqdpjaJicgmco5ymvK4JAi99oXxXH3IK7yjFyg0cexOKVSEtNo5NsyqpAti3HPbaTpOKCRDbSdFtISPDNpagbyxyY8crlIItpDnwFn16O+4hPFUc5T6OPbjkqPS0Lc7O3OU2DbSFhHhjkoTUpjlKx4C3ObYBtpC5HgP+DoqXSiNEBsZ1LUNJY5MePWxCeGRk5UZEd7pLFGLTktgW9AlxzmKP2lYngIkw3e1rNqjEm3eTGsytg1qWVr3CcWYZOUdVIfOJG61qQOkuNrVwTHN0Gkq/wDGYsoKh3lARe5BHFcc5TmR3Ukxfp7h4Nyi85SV+0B+kW0VSu84Yu0BjxTOLlFYTwbEWWDdDNHBsZbmeJ/4MlDZ9GvWPAW5zbAxfc+A6BgkXWJun9M6h0YHTUXD4f8A3LRaN/DbAPHNNJeC1JmEyTiTHN0Bw95co5uhMp4qJi6phvuoi2nODuyEc5S318XDFtvHJQgg2pXXJ3DOotF/hornx/8AmSkUoixlFUcTnNUedjDdvE9EySLzxLmdQ6INZLh+Q62ITwyNHflRkb73UMczHIlCAVKUZADXHLU0pfpSbavoNwU0RpPeVE/thTuSgRfpaljYQIApDQl2kfSOUZWFp+UXQao1wC7RW1HaszgFVGbrHskxcC0cFRNDwI2KsiShI5HDRUoqt2EqVKOcpNHRwmY53ygfZb/zHOUikq4SEWsrX3nDFnk9nxtjm6Iwng2Iss4dHS6RqU4ZcNWSlUojzaag4nOao4wYb+J6Cl0sjSUGx4WnOUidlHQEfr+uShsymC4CeAtzqHRBvcPyHQUqlSsbQEDic6mu+jXqDwsycqRepCyrwFmdS6RqW4ZcNXQttJxWoJhplOi2kJzqRbdZk2PDriOGRvvZUZGuPVlPfwEVhxwEKYSebbPvOaHWHChY2RbTXQNiDV+UW0ukf+wxZSXD3jOJPoBHaTZBUhU0wcjJIvv86f06jTKRrS2ZcTYMrK5XnyXD+mbbFLf7bhlw6CiIItUnlD45pJwEUikH964VZKTSiLGUVRxP/wAzqTbda5seHQBwi19ZV4YZtKpB/dNlUTOMSGMUWj/w2wk8c2mva0tmXE2dFR56LM3DnOvq0W0lRhx1Wk4oqPj1xHDI33sqMjXHq1JVvH6wVHE29FMG2N8UejjF1YTCUJ0UiQ6ixRRi+useAyNtJ0nCEjxhplOi2kJGbS39aWzLiegYYTi4sJhKE6KRVGbTXBpFFQeNmUOkXqQsq8MM1144NpKocdVpLJUc8M0dBPaXqQIZozeg0mqM3khpUhYT4Y5KG0RNNasrgLc5ijDF9czwHRUylnXJsfPOdT6T5DY/XrqOGRvvZUZGuPVqUnh+vSu0oi7R02cT0MzhvjnKXR08XBFtPbPdmYuqfX3W45uhvq7xAjmqAgd5ycXGqMj2Sf1j70Ed1Ai9T6R4LlE3XFrPrGeRiYuszdPhnMUYYvLmeA6BtZF1hJc+mdRKKMXFlZ8P/uQAYmKNRxg02E5rrTSVKcfIQAkTi5QKRLemUTNHQjvOCOcpFGRwmY53yh+RuL9IpK/ECLaOtzvOGLPJ7HimcVGm0to7KRIZ3JNmbNFuDerXkpVKI82ioPH/AOZwZGFHbCfE29FRUyvLFc+OdRaIDY2mufHrqOGRvvZR45EHf1ZaNS0/KHkdlR6RpRF+kc4f0zqRRqIGeTaNWsUzJj73U7iAIveUKT/7CI5x1au8qfR0qlkWrVyY4DODQwo6AnxPQUqlEWuKqDgM5TYwo6Aj9f1yUNsi6lXKHwt6mWGVftrws9QbcqHJXqQor/T9M0k4CKTSD+8cKuho9HGLiwmEtjBIkM6mOzu16o4DrghPDI1xy+OSew9Wac1A28Ir6nU5gShJUo6hArNcmD27IK3aQhIGMGqZjbmUejDF1YTCUJ0UiQzX31YNIKoU4rSUax6ahskXqlZXE25szgMYpNIP7xZPQUJvXUrHibc0qOAtikUg/vXCrJS6ZLRAbHjaepKZo9V6m7PRRx+kLefWVuLMyo5EoGKjIQxRxg0gJzaa7O1SKg8bOiHlFxJFHY0T2lRPNpdKPoIJEW49cTxysjKRvyOdXSv97R8cqXnpsUXadJXCKrDKS8dtp8YXSKQuOy2ME5rtLIusJkO8c4MDSpCpeAzebo7q+6gmLtAf8Uyi1hCO84I5yk0dPCZjnfKH5W4pFKcfpDhbTMWgTOrMotG/iOAHhrzqW4NJSaieJ6CjUcfvHAmJDAYZtMc9JSeTHjZlYMrz5Lh/T4dPNxSUDaoyggPcu52WbfjCm2P2Vk9k3j45lEErrZ5Q+GdRUJ0Fu2+7LZad0c3Q6Qrg2YsoDg70hF4UdvvORztOo6e6CY5ynuK7jcoClJdpBH8VVnwgJACUpsCRgM2UM+T0G85fX3R11ob8rHA5Vjfkd4dX5Jfm3rphxrYbOEJp9ORdxaaOveYnis6KYXSH1xWVYgaKdmcySL7/ADp/TOaccpZbabTIICJxfpFJX7hFrC195wxd8nse0mcc3RmUcEDOYooNry5ngMx2kkXaOj4nOolFB0iXD0BeIso6CfE51DogOmorPh/9yNtJ0lqCRDLKdFtISPDPkpSQd5z/ALXR6bSTQl6SAs82fpE3FqUd5nnU2lnUA2Pmc5TFJaS6yr0TFlBB7y1GOboFGH/jnEkIQnuplGkejdpb5klA/wCCHaU9pLPuGzrrXHKlOwZXBkWN3WKPTqUnm2bJH94fpBWvAQt95QAHwEbGk6Kc6j0ZOLqgmEoTopEh1DkgbKOgJ8TbmF4i9SFlXgLIwy45Hkg3WQG/r0D9JOLzlnAZzqQbrCQ3+v65KPMXWZunww+OdRGaK8tpxyalFOyOcptIVxcMVqyp7Zxybp/bGRe9Ybc5TTqQppwSUk64kmaqK55tX6Zy1a1Oq/Tqin6QoIbQJknVFVuaaG3oJ7W89eTAyK3DKrhkMLGw9WqWhhFriv0gJSAhpse4RZ5tOjHItnmk4ntHPdpahdo6ZDvHosMuOUqOAikUk/vVlWWQxii0Yfu0BOa46rRbTWh15Wk4oq6Cgs6+TBPjbmrcVgkTh59WLqyr35KbSyNjY+Z/TNEONg3WEhH6nK3SWDJaPjuhulMa9JPZOzOXRKQLisD2TC6O8MNFWpQ25rzOtt0/EdSw98VqQ5zupsaRi/zdHTotJ/Xr54QMj535Ub4GR4et1VDDImtXw3wlhrxV2jtg0ds3Rp74+ztnnFaUtQ6BlRF9/nT+kYZcc/Axzi0J7ypRfp1GH/knH3yt3EExcbpLnsyh9hiiLQXUlNdS8MyhtyuhVdXAW5z8tJ6TY8ego9HQNJV7cNcSGAzaUsaTg5MeNmWi9p0cofH/ABmqWrBIikPnF1ZVmXiTRXbHE/rCXGyFIVaCM7kjJNJbtaX+kLZdSUuIMik6syTplR37qz2dhium0HZ0eiYwjVGlE3nkoHrKAg1XeWXsaE/jBRRECjI7WK4LjilKWrFSjOf4AtW/ITuh1W1RytHfAyL39U5JhFY6zqHGJJvOq03Nv+IqIPOr+EFw2q9EbTCnFmalWnPo9GT+9WB4a4QhOikSGc40pbq1oNU1ERzdDfX3lARzVAaHeVOLv2dvgiPvy09wSjnKbSFcXDFpJ4noKZS1DRAbH6/pnUGijCRcPyy3UlXATjm6FSFf+Mx9yWkesQIvqo7fFcDlKe0O6gmCmjglxWk6rE5soolASf8AVV8h+uRplOk4oJHjDTKdFAkM2luA3lpqDxszk+TKUrm1eZUdR7OdMQadRk/tTQvpHpp+uamjUsKeoowI0kQFUWkIUdgN73Rj741RiItVF+ktJ7ywIvU9nwVOLHVL7qDHNUZ9XgBHNUA+25Fyi0dPGZixxpHBuLae6O7ZHOUx9XFwxeJPE9LYCY82r3dIehB2nI6rYMwHfCTuyNq2iXUJa90c1RHSNpEoHKKZa8ZxOkPLd9UXRHIUdKEhPopgonMpEz9IW+6reTsiYBqixKRE1IKeOe9TFC6wmqO8c6lUo/u0GUFRxNp6ZlXpv84fHD4ZyE0toqqaKkmREWUKt31kxzVAoyfYi6lCeCY0j0LtKeNVtsTJh+lL/eGwbBqGRlRF1gFz6Qc2h0MemqufDOnHIPn9sZFvrDbGEaovLAjnKWynisRfp7XgqcTS84s+qiFP0BK0IctUhQlI7s2YJB3RJunPgbK04+/K9wi/T3/BUovvuq4q6awExYw5+WPMEcYtqJ4qi8+geEXqQrwTFvKK8Y+7g8YusNj2YsAySW0hQ3iCqjGorsnAwULBSpOIPQS6Jobsj2+zNb4ZEq7J6UJAJUdQgFxIo6NrmPugF5S31b7BHNtNNAbpR5ysfVi41ZtWYFGYeBUexgBHIMKFYabh1RyTSCU2krMJTSq1tvJ7eMFigoQmWKkj5RWWoqO/PZmOce50+OGcxRAbXlTPAdBdBPCOboj6uDZiygrT3iBHOKYb4rnF6nt+yicUhltZWhtZQFHXKDFGaGCUgfDqUzox9goqv2Zs31D0z9MtMph9JXJjw/8AuZomASLIcQDdYSEfXPRSKOqq6jAx95Ce4gCL9PpB9qLzriuKupWJJ8IsYc90TLBA2k5QEgknVALpDQ34xeCnD6xi6w2PZiwDpRSUi8ixXDqIG0wBkSjtHNKeycjg3dIHXuYY2nSVwEcw2lO1asT4xJJ5RXqwb4aRu+secU8rdEmwlse8xeWtwmFWgUhfnF7NwiWi12dvGDSnRzi8NwhSEKNU4nb0FGow/eLA8NcJbTgnNELbbClJo4Ddgnbrjm6FSD7EfdKnfWBHOPUZv2pxzvlEew3F9+kue4R91Wvvrjm/J9HHFM45tttHdTGkYxOSlUk/u0lUEnEwYor4wWhKvh1CyFPUlxKG04zOEKolAmij4KcwK/8AGZRWH6aylZTWUAbZm2LHFr7qI5qiPK4yEc1QE+0uLjVHR4Tj70E91AhbzqqzizWUTr6uENpKlHAQOXcVX1hOEWoUeKoso6fGLrLY9mLABwiZNm+CyyZM6z2slVFiRio6o5tN/Ws4nqNIB7B6i0N+VtvYJ5q0bchEKGw9CEgEk4Aa4TSKakKf9FvUj/MFIvubIK33QEDVgIq0ZM/WVE3XCrL9rd4Nj9YrKPAbIAJknEnYIqIsaHx6F6mkWMpqp4mDnWSHARpH39GmjjSpCgnwFpynye6rnWNHej/ESPRYGNExgIxETpNKbR3lQU0BkvK7SrqYrUl0qGpAsSPDq1VlNYi2LQhPFUXnkCL1IPgmLVOK8Y8zPiYZaabSiyZlmhttM1GO06cVZpJNm+CyyeZGJ7WQNp8TsEJabEkj49SfO1NX39RKtgyu7rM1G+zK4NtvQWY7oFJpAnS1YD+H/mC1RzxX9Ilpun0frFZ1U9g1DMtwiZ4AbMhGo9BZFsMTEnHecV49SSIYo4wZbmeJyofYWUOoMwRCWqZVYpG/RPAxWbXWEao1Rqi8uUc5Smk8ViL1NaPAzi6VuH1W45uhvK4kCOaoSB3lxcDCOCZx98UnupAjnKZSFe3EyZnpbrajwEWUdcebCeJi842n4xepHuTFlIP5Ym2pLnwgpWkpUNRzH3dt3OeOpJq5gbbHE7IqotUdJW3NJUQANsFlmYZ/3ZQsjnHbfDobYvOoHFUW0hHhHnFHgmOUbnKcrc5lnWo1uolXaORR2CHF7TmoXsMJORtzw6AeUH0/0Un/AHQWGT31fpHJNWvf7YJJJJ6aes4ReMWRRqLqcVbw1wlIwHT6JjVHPUhCOKpRM01ClereilUkaLirs9mrN/Z6S633VR98KuKRH3uXBAi/T3/BUovvOq7yz00kIUrgI8zLiYtLY8YvUhPgIvPLPCLUqPFUWUdPjF1pA9noKqxe9FWyFtL0k5WRrVeOatZwSJwpRxNuWqjRGko6oDbQ4nWc0qUQEjEmOSamGB/dlaa7RiQwzeUc8ANcc2hCB7489LgIvUhz3xecUeJzGU65Vj45yhqbu9RbG7I6dZGe3tlkKtabc8JM+QRecO7ZAYasWRq9FMVUeeV8N8E6+lqpSVGKz35Y1FezZEzkpNOUNDm0cdcHocDGqJqWBHPU1kHZWjm+VdI7KI/Z6D/7F/SDULLQ9VMc7TnzwVKJqJUd9vULEqPhF1hw+zHmZcTAQ5KtKdmZJtNmtRwgFznV78IkkBI3dQZd7QkciED0jKAkYJszXBrXdy9loYqgNNJqpGaVLISkYmOTbmmjj+7MU5/DTnBudjafjntIJABVbOLaQ2PGPPT4JixLivCLlHV4qi6w2PGLOTTwTClrN5RmeoITvyttbTnqb2ZFJ2iCNmaltArLUZARVxVio9tUKeWZuLwG2DSaUqo2rWcVcBEkiqjZ0V1BlE1FKB74m4pS/gIqpKZ7G4k2OTT8cyitnTKa6uJzMIwi0xz9KaR3lx945Q+omcfs9DdV3jVjmWGWuN6DOmrSNiBKOdfdc7yieprceBInIWx93T4xdYbHsxYAOGV9Wqchl5V2YY/3QEISEpGodSo6dcyciDqbvZzDXFRyV1zSwNe2EoQkJSMBmqWtQCRiYqIsYGA25pc1uKznlnWrrxV2RlKdSLM9PrWZXNirc37a+JOKF0H0U7YkidQaI/WOVpMnqUdFv0UwVuKmr5dEEMpFY7BCVUkl9/s7I5tCUfGOccUrNorHoqVNXAYxIYRaY56lso4rg1VrdPqIj9moP/sXBCFNMj1Exz1MfV7UT19FdQo8BFlHX7o0AniqLzzafjF6kHwTFpcV4x5gHiY+7t+6PMAd0ygqozk/VX9YKHElKhqzGU65VjmuOH0UzgnJb5lGlASkSAw6mlsfu05H3dt3Od2IuwHXphnZ2oCUgBIwAzVLWoJSMTFUTDKcBnMt9lOcpy8hRxqxdpCvFMXX0niIs5NXtR5gnhF5hwezFqSOsVu0cilbBDi+0c9K9hhKt2RtzwzBSXk/s6DdHbP0g0do3fTV+kck1Iv/AO2CpRJUcT0VVAs1qOAio2Jvq2/OCpRJUcSc918UcPOqTVTNUgNsENqaZHqJg8vS3lz2qs6C6ys8BFlHX42RaEJ4qi8+gcBF99R4CLQtXFUWUdPjbF1lsezFnRFQHPItT9MrTXaVEtWbV1uGWSQxhDfpYq451pi882Pai2kDwE4s5RXsxdYUeJi6wgcTFlRPAR58+6EFWkRbnPO9pWRka1XjmqUcEicfaqSLijWCdsSGGapxxVVKYlosp0U5zCNU5npbQDxi8y2fZj7unwjRWngqOafUO8ImtE0dpNvUgBiYSOyMi9qrvQhM7U2ZFpy13JpoycT2twgUdmSTKVnoiC0wec1q7MTPRBSrjPa28IDLKRympOzeYKlklR1nqC3nUJVMyTOLraBwT1R9AwCjkU5qbTnNNdhM8iJ6KLxzi6q3YNpis6sn1dWeyjVOZzn165SGRCB6RlAAwFmaQRMHOU44qSRHZaTopz0cD1P7SyJI9JOzqKdibcqGRqtPQ8n2sq0xyi7lH+KuEJSgDYhIhTbar50l9FVZbKjugLpJC1dnUI5OjSKu1qEEkzJ29QAGJhprsjqtII7ZyFzW4rOeXqrSGR57aaozmWdgrdA86fREhFpA4xefbHjHnx4RZyivCLrKzxMBoNVBOeORJ1Nit0anXVVUiLbrQ0U9A26PRMJcbM0qw6k6g+kk9RLnaOQmHV6p2dChewwlQyNcobsjMbd0VW5VyJJA1QkJXWWoXlbOh5pskdo4RWpCuUOzARybclEeg3EiarfYT1JE9Fu8emvOIHExNtaVDcc5bhwSJwVHEmeRlvspzXXOyk5WU6yKx8c59WoGqOgklagNxi0zz3ne0avRKVI2CdkW3W06KeiuGaDig4RzlZpW+0RdfbPjFhB4dO4o4BJ6gAMTCU7BkcOs2Dowk4psyBxOkm0QSTNaugrG4yNe2K7slS7WAiqymuR4Ji+5d7IsHVFOnF0/DOKlEJSNZi2ko8LYsK1cExdo6jxMXWEDiZxYpCeCYtpC/CyLzqzxOQy0atucU63TLIyjVOZzqmtxUsjbQ9JUokMM113spifUGU6yKx6M0loc2rSGw9LZF15wcFRY+rxti2oriIvMJPAxeZWOBi0rTxTH3hPjZF15tXBWe7tXd6gDqRblSyME2no6hNisqtirRnh2kiSdTe3jHJtgKWPRGAibivAYDqoSMSZQ22MECWcloYun4dC492zLObZH7tMzxOR17YKozm2uwnIXDg2nOS3rcV1BtvtKlAAwHRqQoTSqwxV9A6J6q4FElCVXc5hn2j1CetWRSjgBDjm09GlYxBhCxrGSuMU5oQgFSjgBHL0gpKxtwTBQzNLe3WerhZ0Wr2eU6mhV6FlrWE28c55zarI3tXezn1+tkLmtxWchoYNp+PUArU2J5zaFGSnLE55bVj6J2GFNrElJzptNKUN0eZlxUIt5MeMWvo8BF6ke5MXnHDCW2QcJmZ6Bka1Xs53Ym6OnSnbA3ZKgN5yzpSyTanIQcIU2fRyhDYmflFdRvnFWs7hFt1sYJ6xyh0nTPwzlLOCROFLOKjPoGUapzOc8vXVsyIQMVGUJSMEiWa852U5JQ032UjOdd7SukutLPARZR1+NkKQrSGOR13tGWc2gHzafjEleeRpb9+fyrY55H9wzmtqr2c8vVOzPCUg1fSVsgAYCzNWs4JE4Uo4m3py54DKoA3G7OlQvVgYByB0cDkkLE61bIsGOrWqKzht+XWENjFRlCW04JEs6oMXTLw6EvvCTqhYnYM5tkYqMzkCzg2K2clv+IrI0nUDWOc+rdIdAlAxUZRbXV4x5gHiYu0dv3RYlI4DItzsicEnE5GUa5TOc+v1oS6jEQl1GB+GeaS0LitIbDmIQMVGUBIwSJZqgDIkRa63FtIH5YteX7otU4fGPNk+1H3dPjFjDf5YkAAN2coa3LvThIxMJTsyOOa9UEnE9MATeRZkUk64v2AQLOCYKlGZ6yXTg0PjnhsYNCXjluMOHgmPNVe8Yvutp+Mc4+o8BKLUKVxVE2mUpO3PWsaAsTwyOOdsyzg3qbTkee9kZzTO01j0CDqRezlDWs1cjTfaVG7Ndc7KTlkrzS8d2+LMM4pUJpVjEv3arUnKg6kCt1Tk0m41Z49PyhwTlTRwbE2npwDorsylXa+cTJt60knSdvZ6nHFOLUozxixhJ71sXW0J4DpPs7Z5xeluGVlvWBbnOudpWRvaq9nL2Iu9A66RpWDOaa2CeQuam051X+IZZn2Vw2jQ+meW1eB2GFNrElJyO9zqdRB59eG7f1BKffkW4rBIhbisVHp5iEq14HIoa9XWm2h6ZlASMBZ1SqmSnzgNkFazNRxORlG1Wc8vXVlkSgYqMoCRgkSzSYWs4qM8t1pZ4CLGFeMaCRxVF51sRN10qGxIlASkAJGAznVA2AyGRa+2rOab7InmApMiImfOJ0hn8ogc8j4jIkHBYq9SKUyU/2dnGFLWZqOPTz9FOVNGSd56jyZN1eWsMF/PrK3jggSHHqZUogJGswW6Lj/E+kFSiSTicrrvZEs5tvtmeRKtTYrZ5UWRMxZR2/dF1KRwHRrX6RsTxyso2JznjqBlmpcT4jbCXEGaVZ/2lsXFaW4wCMYCVWPjEbenvOgnsptiqwOTTt19RA14nItxWCRC3D6R6iFDEQlWvIRr1dZbnpLvHqF9xKeJjz1Y+qJxJhnxXE3XCrdqzUnWs1s6pqbEsj6uA6mVKMgNsWebTo5GkbVZy1dkTgk67c7kXDzS/gctqh74tdb/MItfb98feEQpKnQUnGyFBpVZGoxMEgwA6kODbgYvBxPhOPOn8saaz7MWJdPhFjC/fFlG/uixhPvixpuMG/wAsaSR7Ijz3wEfeFRfcWriepVjopyijJONqup8kTdXhxieSuNFXz6u012jEhh0d9aU8THnq3dE45tlSu8ZRdCEeE4vPr98otz0pGJMoSgYJEs2Zwhxw+kZ5H065g9SKlqCUjXHJtWM/PKpzsJzLVJHExa+3+aHENOhS1WWdBLl3Jd6LVqPj+DADEwB78i3FGwCFuKxV1MEYiEnXryFMFJxHVnHz6N0Z81EAb4tfST6tsc20tXGyLiEI+MXn1S3WRaZ9IjYi9nOnWRVGUVjcXdPUJkgCCEHlV7sI5w3dSRhmKDVUVt0ee9wi19z3xatR8fxLlDgMMooyTYLT1UJJuLieTlk+11ZpJ0jeOe6Z3QZDqLrvaMhnMtbb2YGaQZSwX9YmCCN2daffFrzY9qLX0+EaalcExY24Yu0f3qi60gRYpKeAibjilcT+NBIxMBIyLWYUtWkerSUbybDkUkiwwpB1dUaRqnbwz3XNgs6kynXKZzlDUgVc3m3FJi2orwjzTcWBCfCPPEcIvPuH2otUTxPR2RY04eCTF2iUg/8AjMWUCkfkizye94x9xX4qEfdP7xH3dP5xHmm//ZHmmv8A2R5pv/2R93T/AOwR91/vEfcj+YRbQHfCUW0Ckfli2hUn/wBZi2jPji2YtbWOKev2xZm8qRwy8mk3EdXSr0TjAIycokXk9UdfPdGe0yPSvHqKAsyTO0xY4TwEXWlnjF1hPiYsqDwjz6hwsi86s+PS3UqVwE45ug0g+xH3Sp31ARfXR0e1OL9ObHdRF+mvHupAi85SFe1H3ZSu8sxZ5PY8ROLlCo49iLrLY4JiwDp9eS1I90XmWzxTF+g0c+wItoDI4WR93UnurMXXqQjxnHNU9XtIjm6UwviCImGW3O4uL9BfHBM4k4hST6wl1oIGGuBkIT5xVg6zyCsU4ZJQZaCsN3U2m9creOe4Ropujr3NUd5fdRH3QoH+oqUc7SWG+E1Rz1MeV3Uyi1lbh9dcc1QWB7MXEITwHRYReUlPExfpbA/8gi3ygx4Gcfe63dQYsL6uDcXaNSFe4RdoDnisRdoCfFyLKHRxxJixqjjwiwsD2I8+2P8AxiPvfuQI+/L8AI/7g9H/AHCkfmj/ALjSPzR/3Gkfnj/uNJ/NH/caR+aP+4Px9/c8Y++q8UiPvKfFsR51o8WxF4UdXsR92o598c95No6/GOf8gtHelUo+501g+osGOa8pUhv+sx9I5nyhRXfEpPxiSpeBn1QAYwNuRSlGwQVnDV1lK04iEqGvIpJhSVC8Ootp1C8c913YIn1nmKK85wTAK222R/qKidJpp4NJi8046fXXHM0NhHsxZhu6C+62nvKEX6ez4KnFlIUvuoMc2xSV+Eo5ugH2lxcozCOMzFjzaO63FvlB7wsjnKW+ri4YtUo8TGA/E+U92X7Mg2el1uoo3FZeWQLRj1Fx4+ldGe2yPTMz1YchRHVDaRIROkPNM8Lxib7jz541RHM0NlO+rMxZYM7nH2kd5UXqYhR9S2ObbpDnBMo5mgfnci4ijt+E4tpq09wSjnKU+risxbbx/HKurXlUfSOEFSsT1zk1G+nJIxYObVhu6ffDTXZGevYi71Kqwy46fVTOAXQ3R0+uZn3QDSnnXjsF0RzFEZTvqzObN2kNI7yotpaVn/TFaOZoz7nG7HM0VlvvGtH3rk/6aJRztLfXxXFvx/kIJGOUqJsEFXoDDrqXE6oSoa8ikKhTasR0yJ6KLxzyAa7mxMFRxJn08qNRnHOAsgGlvoZHZTeVAKmS+va6Z/CKraEoTsSJZZv0llviqCA+p0/6aZx+z0JR3uKlB5Mssj1Uzjnqa+r2pRM2nf8AyRyi9JXwy/Z2zZr6+Eq82qBkrpF9Px6ZbxxcNnDNvqvdkYwUp5tvYOmq0Zhx1XqiAaW6ijp7IvKgEs8uvtO2/CKqQAnYImtaUjaTKJKpaVq2N3oIo9Edc3rNWDyXIsD1UzMc9TX1e1KJnHf0ckpUrgIu0VzxEotQhHeXF6kNDhbF6lHwRFrzx90W8sfajza/zx5k/mjzSvzxg6Paix14RdpZ8URcpDR42RdbQvuri/RXR7MXgRx/EeUULicN+WqnzisIJOP4AGVnhlLyBdOl0gAxMIbHoiWWu4oJTBTRhVHbOMEkkk7ekkBM7BAUW/s7R9J36QFP1qU561ifdEps0dsarEiCPtHLKGppM4IotD9p1X6Qf2nkk7GhKJuurcPrqn0cgCTuiaaOUjauyOfpKEd0Ti+px08ZRcobfiJxdSlI3Rb02JiTiEL7yZxeoiAfUsjmaQ43uWK0Tb5N8eqqRiTzDjfEfhgTq1wABYMilqOEKcVr/AQpOIgW3shBFkWebOHRpJ0W72UpZ5xz4CKziiT0nJ0Zlbq/VEB3ynSUMI7CTb74lRqjjo1tiur3x+yUL2nlfpBBpRbT2WhVis4tS1bVGfScwwte+VkVqS82yNgtMWpXSFethEmGGmhuEWrOXnKQ2n2oscWvupi5RlnvGUXGGU8bYsWhPBEW0pfhZFtKe/NFrzv5zAUHV1hga0JZphCV6nNR45CDaDBeo7zxo+sVzNH+IspTv5o+8qPECL3JL4ojnKKk91UX0Oo8JxdpSAdirImkhQ3HJI3hsNsc5RkpV2m7sE0SkjuuiXxjnqOsJ7QtH4QEJxMAe/IVHCJJPNpw/AwoGyARkKFCwwUK9+3oi7rcPwis6rgNZgpTca2DX0kxUQjWtxVVMVni75Qd7KLjfvxMclQm2KE1sZTb74rPvOOn11T6Wqy0txXqiK1LeQwnYLTFxjll9py2JCSU7BFsTNg3xa8FHYi2Oao5O9ZixaWx6iY5x5xXFXRpotIVcNiFn0d0SOTlmR+zqOHYP0zptqUk+qZR5+uNjgnHP0bxbMSD4SrsuWRMSI3RYYm6wEr7bVhgqojqX09k2Kio62ptexQl+CSGMVlaZymjtHvH8FCSbsAjJL0hgYKVCSh0CUJxUZCBR6NJS0CVbUIK1qKlHX1ubbUkdtdggGkrNJc7KbExUYQhlGxAiZx35bFqRwibjj6zvXFjjoi7SPzJi5Uc4KjnWlo4jpPszp55Auk+kMikLFZCrCILeKDahW0dDNl1aOBiT7aXBtTYYkHaquyuzJUpDaXUetBVQnKp/hufWKj7SkHfr/Ag64LdW7LyTZ5xUTOP4MG1mJjJWT5wfGJHH8CCyORZ7bkTCPtD3acw90WmzYMl5SRxMWvt++PPj3R574GPvCIuutq9rMlq2GJ1OTVtb+kFQ5xrtJ1eHRJcbMlpMxDb6MF4jYdmQt/vReQd8EGwjEdAHHiW2tUsVRcYRxVeMS5JuXdESlViwjIW3EpcbPoqtgroK6iv4S8PAwW321IXsPXw6sXdQyntnAQVrM1H8HBGMVFY5S62L+sbfwCsBybP8RcTQjlnv4i4mozySrV1bExJPNp3YxM2nfnc26tPAxfCXB7jElHk1et9cwqak298DCm3ElK04g9D9nUbj2G5WUUpGi5YrvZ98TbbvHflruqkPnHNNhI2qtMeePgI87W3KEBL45JW3VEwZjaI5KkNh1vfqgu0Il5vW2dMfWN/XKyhzY+MDIVqMFxZ/CQpJtEAE5S82O8Ou1GEFW06hAU9KkP/ANoi3DYMkkc4vdhElKknsp6SSTWR2DE2zeGKTiMsjY4nRXshTaxJacegsxht70jYrjkcZVgse6FIVpJMjnPq1lcsql+jgkbBm805Idk4GAl3mnN+iYmDbE3OapH8VOvjFV5Nh0VjRV1q3QEAAZFKUZAQT6AwH4XWT4wBPKXWhd1p61ZjAdppKE6mxpGA2ygNtD0RkKRfc7I1RfVJPZGHThbailQwIjY8nST+uXlUDnmx+YdCujnBy8OOVLwFjlh45z6digcjxGNQwM8J02uyf0jm1XtaTiIU06gOMqxQqFP0SbjGtPpI6xsb1mAALMhUoyAizzYw/DZjDWIFuUutC7rGzrFRlM9qjgImnnKRrcOrhEzjE3FS2DWYKUc23uxPUkuoxT8YS6jRUJ5VlhCeSVaJmUovONJ8ZxfpfuRFr7x4JAjF8+0PpHmlni5H3b+8x91H5jCXG6OEqTaDWOXknQauNkaTw9oRdpDg4pBi7SU+KZRZyauCoto6vC2FtOIUnlE6xLDIpO0SgpOIs6AKSSlQwIgN0qw/xPrAUk+6C9RQluka0YBf0MFKklKk2EHV1X1NZgACQGQqUcIKUnmh8fw+fo64FuUutC7rTs6sHXSW6Pt1q4QGmUhtoahEzBTR7x7ZwgqUSpR1nqEkgqO6LGSB61kc48hPC2Ly3F/COTaBCeM+rrdadArGdUiPNVu4ZxJaVJO8S6AJN9ns7OEBxpU0/KL1ykDRd/Qwpp1NVaep7EfOAAMMhUowUIPNfP8AEQZ3YFuUusjinqgepY7rX1yVnDwG2JaLXZHTSAJO6PNVRtXZHPP+CBHmax9e2JJASN3R2uIHFUW0hn84j7y1+aPvTfvj703AQ2+hSjgJ5ZkgDfFik+/oZKAUN8WILZ2oibLiV7jYYk62pHHOrtKkfgYsuujFEcm7YRorGKYLbot1bCOohShzfzgADIVKMhBQg818/wAT9XZAtxyl1kXtadvUQACScBAddkqkahqR/nJVTee2bIK3FVldJVQkqOwRNcmh62Mc5WdO+wRJttKBuGffdQniqPPA90ThK04KExleb+0LCUqIErIvPOH2otJzm3R6BnAIwOR5GurZksWoeMWUh380eenxSIvNtK+EIbWzUrGVatnyIBG+JpHJK9XD3RMJ5VO1H0zQtCilScCIqLkl/wD3RyTnsq1pMFtwW6jt6cLcFzUNsWDIVKIAEFKTJv5/itmjrEC2c8pWixz5wQoSI29MEpBKjgBFZUlUg6+zuyFqjm3Wv6RPo5hFRPaXZE3lF07MBFVtCUDdmzcUlI3mUedrn1BOOaY/OqLFJR3Uxzjy1cTlo59XLSe/0LW1Fw+GV5rUlVmc256WCuORFJZWpJTdVIxapLg9cRzzKk702xdeTPYqzM5xu92hjBLXOo+MEHHflBBkRsgNPWPaj2o5Ndh9FWwwWnBJQ6UOPDgmLMhUpQAESwb2fi804axAtyzwXqVBQsSPSBKRMnACJmRfVidm6JnCC0yZN61droqqElStgib6qg2C0xzbYrdo2nNvPJnsTbHMs+K487UGxFkTUSTvz6Pwy0g+uehfZ9sZUOdtPyzuRUbjvzyLbVoqEoW0rSSZZeadUBs1RJ9qfrIjm3RPsmw5ecTe7QxgqTzje0ZnJOnnhge1EsHU6Kv0gpUJKTiD0QSkTJgLXav5ZSpSotsRqH4zWSYkTh8MslDgdkXrU6lDopDGK6/Pn+2CSZAbYLbdjI/u6Gq2kqO6J0hXsp+sVWkBA3ZkluitsTbEmGpb1xzrqiNmro2E+oMriu0onoWTqVdypc/hqzpjGEuelgrjkTSU6rF5wFaujsriS+aX62HvylSJNubsDFVxMj88gIMiIqr88nHfHLNjnk/3DoaqB47I9bWcptmvUIrrPh+NhSTIiAg47PpAIOQggEHUYKmwVN7NY6EPuDnDog6t8Ek2DbHJt2Mj+7oAlCSpW6AqkK9hMVW0hKd2Ws4sJG+JMoKztNgi84avZTYOlQ2MVGUADAZHl9lJ6IKGKbYSoekJ5H0ern1VHmnLDkUhQmlQkYW0rVhvGfcXd7JwgJc5pe/DIUOJCkwVtTW38RkStJkpOEVhYoaQ2Ry7Yuq0txz5maUfOAAJDKUN2rgqWZk/jwSs27dsb8pW1dX8DBSoSVvzuWcHNpwG05OSbPNDX2ugCnbiPiYqtJqjLzqwndriTCao7RxistRUrf06TqbFbLU1uKl0bO0XcriOyojPqqPON2Hhk5RA5xv4joZA1m+yYumS+ycchWxJLnZ1GCCCCNUBafEbYmLyFiCn0fROYABM7oCnsezlmTBbYPExM4/yAAomqNesQLRbhFmS8PHZFomjtDMq+iNIwEpEkjCDR2j3yPlnyQOJ1CJ6bnayzdVLdriTI5MbdcTJmd/UVOnFw/DKlvU0Pj0bqOyqeV71rc9Lgw1jaISpJmlQmMlZA5tdo3dDMGRgIpH5/rAIIIOyJ6LgwVBQsSUIqqPNLx3b4KfS9EwQRaMlwWdoxhbtOUlSpQQkyb/kO6bNYgCfhrEAg5MIKmPyxIiR2QABaYCBjrO0xyaDzqvgM8Leuo2azASkBKRqGSstQSIqsCqO0cYmSSd/UkoTioyhDacEiWRS1aKROFuKxWZ9GtHbTlaV2k9B9mWbDofTIps+B2GClQkpOPRS0mtaYrtqmPlElWKGCtkFCxbHIqN5GG8QC3ao4gQFPflgAAZZAzXsiss+H8izBkYk4fHbAty2i3UdYgqcmpOpY1QXcRq3wpajNSsc0JSCVHZFZySnfgMtVqS17dQistRJ39UU8cEYccqWRi5jw6Rg75ZWF7FS6AEG0QFemLFccnLp0k6W8dHWQeO+Jpx1jZEnDKWCtkTbugenE8VdoxZkmTBQx74KlGZ/kgDFGyMYsOXmzUOMtUVXE1cyqgcTsiSdLWrbkrOGQ1bTEtFvYOqgDGENaxjxyrc9HBPDpG1dlQOVXqkHoQr0DYqJg2ZLPNqtHRjkQSuK9IvHsjAQLMulbBtknZ/JYUkkGJLNU/AwAcYsyEKSFCCWMOyYKVApVsMSFiRiqKiBJPzyFKZKd+AgqWSpW/qxfVgjDjlqA33bPDpmlbUjI+n1ei5BRw0PpkKD4HYYKVC8OgkBMwC7dTs1wAlIAy2mClq3fFZRmf5Oq6SOyYABrbjpf5iw5ZKTOJITyjQ1DERYbRiDZBbYNmtfV0oTpKhLacE/HJMmwYwper0Ru6ZjhLIpO0HogQbRAXr17jk5VOknS3jPmq4n4xdTbtOMWZJkxKduzXFpknZ/KNkDlJn1hjGlMbYsOW8me/XE276dmuJEEHf1bl1aStHhl5FOku1XDp+CjkELGw9FboKxy2aCsMshjAKubTvxiYTb2jltMG8JbYNSfExb/KlZJIMALuntDD3QK2j2hhFhy30hUTZVW9VUSWkpO/qYHoC1XCN2RTitFMKWrSPTuD1sgh4et0dQ6ScOGQpPhFQJJVsEAumoNmuLqZbzjltg1SDLXqiSb3yistRJ/liaFEReuHdhAKpVe0MIsOWSkhQ3wSyah2aovIMto6hIYwEelirLyaTcR8T1B4cMru+3owoYiOZaJ3nCJvrJ9VMSSkJG6LMpq2/KJC/8BE1qn/Lk0KIi/cPaTh7onintJiwjMvIt2iJtKChsMScbUnpeWV7OWQ01Ybuovd3L7I6GSEFR3RzhCB7zE6tY7VZkyRF2Uu0bBF29xwi+qf8AMFZJIO6OcE/WFhiQUFbsDEpyOw5konVqn1Y5tQUPdF9tQ6GXo643ZCo4CCpWJ6ivu5U90Z0kIUqL5DY98TUC4d8SACRuzLVWxKYRxtPui7+ZcVlKJO/+ZZTrJ2KjSKNxtETNqe0m0RYoHNtaTPdHNrI4xohXCL6FJ4jMAGMVR4nblqp0E/E9SPdyo7uS6kmNCrxjnHfyx5usfWjUBuzJkiLto24CNOe5FnxgyuDd/NVZKiDujnAF78DHnCNy/rFuG0WiLCM61pPhF1SkxdWlXGCSyVH1YvJUniI0oqg2nqc6ijZqEXWFeNkWlCfjALrilfCPNA8YsAHDJhkxi1Qi7NW/VEuUHsWxYPFVsTUok/zfNKiDui9JfHGLVKTxtEWVV90/pEiap3xYczDLahJ8I8yPCMFDxixxUWPf2xY4iPQ98aKfzR5r+6PM/GPMn3x5r4xoD80YJ98WrQItdHgItdPgItrmPNT4xY0geGXDLjGMSrCe62NneMosWT3RKLABvNpialEnf/O0gsy2YiLUD2bI84sd4TjFCvGUXgoRpiLDnYRhGGdhGEYRhnYxa4mLJnhFkhxMecPsiMJ94ziVeQ2Cz+frpI4RpT4xo+6yMVjxnHnPeI9A+MotSfAxbW90ec98ecTHnE++NJPvjSEYiMRGIjERpJjTTHnBGkY1xYPjFkhGmqPqYxlFp/8A4TYY01e+NKMfhlxjGMYxyYxjGP8A/oT/xAAtEAEAAgAEBQQDAAMBAQEBAAABABEQITFBIFFhcYGRobHBMNHwQOHxUGBwgP/aAAgBAQABPyH/AOEqa0OxPplNJ8k2YeZypGaw3Um6odedbHrxXPHawQ2y+JoYZrSn1DmjvuS3/wCCez4TT11ZtOdG90J8sm4dRl9JpHoTQ/ShpBANiUQI4Vw1KToEX2TUzNY9OAUx0WvbKbdS1p9s1Ru094A/+6qZJ2MlPQHnNK97TWivKE5PxNFECbY2RDeI7zUyd2auHman6+D2xPSJbnxP+FP+JOr9IN/pNkemEOmetNL9BNHbswSHMlJeCHaagYPSO5M3ueELNN2XLGknNFbNyo/+zEyv89ylS9pFG2fKBgCuU0OKwTqZqoPMMyvdmg3QeN7ik+DBPjDNYHmOobzFc5cuXhcvC8AN3rDQerNHj9w5uJ7z4Mojb7Sv0d5rZ8zQQwDvLlXNjh9IOQh75DqXoPFZpndFkr/6wtM2K9Mltm9DM2ve80cQDlg6xmYm7sv7Z5Zpp+qOUurlNa98mpro7v4L/HcuXheBNPHmKQVxkSnF7JQQCbBOYw3mDqAYAiM9p5cCJZUPafWeXq09f/qLK3oT3laleo5szDciVU0mtiD5HzLBZzmkB6Re7jmxYsWX/m3hqAdmcj3WUpkRTF36w++zs3DCmqN0FsLnwzX0nfQNfpGURHr/APRFoeu7TL1pbOkOgBsQOgqaQbNCCtF5XNdT1h1sDtCFsuajFxf/AArwGOWKuspMm5ysXuQmzOqAdG8NRIc9oZM7g11lAz2T/wCeZoK9I2dU6vSe8uIVQEqojVFaLuy+RzNok222hC0rzXGoxMKif+LcHBYTV1lCNOcLKAu0U1OsuI1EUQNgL/lnFgFs/wDzTugrCgfdleB7xgtAIZQXNCBvZN5f9RRY09XFxDLCROCv/FvAYzbHZladuz9SSDVh3GaxLiBFy7PVxkB7J/8ALFo6hzdTDQW7phlAMD7AqWPREKorbaopsVbuAS7CC8XylovEGWE4X/xykFtLlcrV1D9QmkO4y5QmcTIddyZqPRIpBE/+TA+0UrxEKoBgcoS2PThlvbcjGsGJVO8qbd/Ezdl4mg3sx8wnU9+eikslDjovbOOhWZtvwAJEwrhr/OqvMRo3UdaQwuXBhXJNrnnRQ6scxl3EmkuCq6fkxCvKr/48m18pkD9MfuARAcjAS1rCmx1ETel3iYC2ZC9C9jebWf8AHpF2/wBz6EbFNRzC5DkXftM2qvdnQp2Yrc566bJWwuejxh9oaFHsay7rhspEiSv/AABTVN4yPM6v0m6z3+Jb0Zipmm7o3K6VwcBgwYfUckJX8EDFGeD8SPMmTv1T9RWV8/8A4wTpFAPvsHrAVEaorqeVrH0uTqlmet4hcsmXqoVvhCrxX6t8oP400a1pvFKV5Zav1EV2imay8H+4QH+gzwFn6bMUdM3jr52eJ3iEa9dWat/MPlAnVY+1KGZsV/Czn+9QaHcxwkSJElf5i/UPWhrU8qznJgc23lCCwNczjk0geh6wSpRyHKUlSsVVjSbxsToygvqMBojnHFs35ShT7kJX/wATchzOxKw9dNIDQCOUDEObE+7n1FrF3UvEiydIACP9tLvgUH/UJlShrbTlOUnCvllllvL23RfHOHr2Ym6kbNUO07GoC5ftmaFM1MvWbYIBRwR19X0ekc5C7rk81FSkfuxBIkT/ADDyJyXLzHmeW1z2i2W1LO+FSuALXpGXUeK4Mtd6E1VkmGVyhCETKUCOnpDUC9//AIYLYyJ5e6HSDlDKGWpkB0stO6IlvLY8RcSDBuEjwN10JspB+SAI+s7qtneXjEze+eaIQj2rfmPtrCMhfXmwnrw6pQ58YoXbHOTlMm0a6uZ3iuGCJEwTGvw1+Td8UWjmg0/s4KucI5x4Uxq8ysq43AYM5nfNmcxIKCnU7bBzk1hE3k8oZT0Wnf8A+Ecg+UyndTQgIAwdGOkZ5IBzZcXgCXTSwcX9lHvLs7XI5zrzg27ucIsm4hR5s5idWLNJ7lrNQmGm+U3D7yv5Fs/cOfsxP2fJsn55XCEOr5FcCVGBa5m55JzmT5rSa8SCJin+MDnN9ap53luK5U1KzlXaCJxVgzOvL+5W2mIy3BfBcyGa7i0jk7pkQNP94WJHcYMWiR5k2edXY9o3YGz/APAibN1kLg5l1ZVQS1UyhaF7Qoc/OLwhDg6kU8Wvv8TO4J5h1OQmfAJTa4M1Y8451a/SIM7Tnlj1p0H7ZcorkV7T4us2dF8gIPm9ZzT1lhXYAzdu7Z72bPeV6M2GQHmgnu7yhBoWjuELbTN8h7yuDAkqAbp6qyiKzaB7kr/GU3oaIqsaxrBwWKhQus6JYWmgS5VxKwvg7iM+EWG0GdqPHWFbKoSNOVljpEMjHgVaE+YjZ8n/AL1kQS2ObvDghlkYAVG6zntBv+mF8BgGBYkERzZZMB+F+ybxegESBnNdezWetvv6x3G4ybTkzx/1jFvd5ShZmO5whd0VNWzc1Zdsz7LfszPLfdraPftFq55w4FS87TNneJkZAB2CJ97IObLuZrOJAUBmuVRKUck/wSWmfpHNisTBiYVcZrBvmwR4BqU40c8Kg4llczPZLQcA1H6gmcFOkB0G8D5Ls7ky/V/9xRvoAlLzWraDIKCNNYwvyEx9040heIhBLJa3Kzu0/c/m7+KixipJlYCh6Ec9Mf3M+RfSMZvfbNeXrHkomcA8Tq/aTQgdboX3KmQ77Yw7UzZHqdcyqAZUaM7s6QCweQtvljDNGimXnnMs2XrJnzi1N0Xz5wUjQ2/+DTkM2ZOoAh4VqdDOPYocjaN1q92BbJZVgmFQc2g3wY58Xe7F/leyCJzeULuGUbl+GFmRl4HwPdYLu/8AaBfBCAdxiFMoZa0SzRp3tGsHZYvAYBCBgGWvPLuT6I+faB05+cHC8yirkGcWoT2CU1Z2g6JF1wHymboGafkg0y9PVKIWxPOTOH/BFDAnAoCDLaWuFRIkCJCzXq+H+D3zAr3wcExANLlZzMycmBoM1nmk95pwBaUOWUz/AMhHGuBdx88s/qzmtAtuXywIEGGlneSpB0Qbi8yPOIM7+R2iV/65SgOyQg/NucGBAZqzZbKN+0YXjIGKr4sy0NbPtELAN55sXBwqvzlh1IqN/wAoQe1zi0cyTJhuWmRBNgmQvhxnOiupzF8g5SgDllHcWUBwWhmwZNgyzW41nNCCVIDO2UxCgn5KwqMqmnWrekBGCSpWDhnyaEVUc6hv+cDw1hQ8Wk5/XK9I+ofEPhmCVgQ4oeUZkDtzggyjkLGO9UD5JYp/9UPQjEK5WB9Q5xg0tD7RYvGECBhOc0Ae0j6m74F88+FkR5jQzjgPfMpa8scAwAEhUZKZJ+MG5M2cri5DKCiiw9yCpeRZmq3sh981KMiBzm/XbGUkc8zM6K96Vffhr8YaOwc/eaRqlOTAW3u74phVx2Tq/SDcyAM8jnNIsTG8NO15GFROHRcl7QWf9VB6h8TwzwGIQagJEEMVRyz3hNkQgsZRdMwaQjCh/wCmcFBzY5D0MM3lsbsfuBuaRYuLgYhAgQQO2s2if5H3Y+/AQjtgBuFJ46/MLS8BBFLbwq2e7fpEUiGbwb+/9wxxRY4G4jmqLrHuxYv4a4Kwsa1MpWrhzqpXR0uC3WUEYmKRgBI0+IwncmHBjgRQW5Bqy62aBLIxI5cHroKH/NRe38TsHAVKjljUCG8yxho84ZZKKOkIVhzSHLagf/RdAG8HWyGRNIxW1oastKXpshYx4ggYBAhlsW+UJ3DB+X6lxZpBKDqkayB8wExSxGxMFF0JYN+hLJwAh3NVNenzL8ra/cK+2r7wK4sWLFi/gpt+G0FDkLJ4h9Tb9fuwWYDYjjUTCo9zM+0uXU6YHvEjgmAyoU6nOJGEDGOLqIHVpjl/Gk8FEmbgTgDLiykzuVYPFqgbeusJElAjoyknsQ5NyCP/AJ6liQ6wZZRiUMzQl7F9u2C8NcBgQ4ZdevoZqaqiz5X9VHGa6ufNCSH9JaP6iWE0js4DUMJLznK1i4QoLgjpsJmbQzoG/dekyTjT2yKKLFixfwVFtxrhqUucLG806wQvUtkS6rfOJKrrWCVHAnmQlSpmHk4sYsWacvbKmm81jwDcPOeRSaRM5UKHD48R1YgGYjslorNgRIkcGBe1BkLWaGHLlnUzU94REiAjowRQjUhSCDb/AM1sxrMA0AyywbwQtXBLi/hGERdvIdYZcvUmanXoTtif4jKOz1Xg9WPIK77N+8Lq06pn8r28zWXqsBgzTpSuEZwAezLlDomXXpzZmH+bb1wLFixfyOAhBm526G/CStachfPWdOnmnjnFuJEwqMSMq4IVCXNaQrIUvPeDRUG5FHU7CLb0NZpch7nSK3d0l4f6c0SGyBREwce8C90ofP5EyntBl93vN4ESJEjg6Mzzn8SGXIZ/iEyBsZcCICJkzQedD+Z/+YByEbNzh/wG2D3YGBVExxriqBKgQSyZ/J9zYm0CVOXBFKvXEHOW0sj008yZ2TpnepEmuUFyizU5JuXI9QuwRK0E8s7/AE2Tv+ksM2jbsNpcuLLwfyBg0U1j04SaNA0zA2g8AdjAhULq0RMVgCgcyxYLb7ojznQ+4mBlLmdOfu/qaMXzjYB2ygeb6sSJElYJHHJ33ygyuZ+6fw7z+C3huBBUSJEwcjBZnBXIpGxVJvOx7cEUjMMmc9eD5/8AKJp9kQJpRtglzt5CW8sJixIkHCpUrEMAhwK+Fi+DNRC+H/bg4VdcPsxeXr3wLFjLT7fY9yAY4F7BFn3PkdRGYI1WJ7TLRP4lE76gV7H7lkf2JXDeN/iV0M9t4oFquMqzesCRkgGiDPvvnHirSVG69c4FxKi1m5BvHRbNQ5jWM3eZpHKiDJWD7SwC5gpiNE95hUq0to5wSktUDQiRwsuZnGMSJEwY4Ov46p5I/Zgyu/zDNGkrKGDAkqox3v7qekhIppSQByaLBFkZFdxl5vDf/wCQamuryJ1zZlRO+gjtZuhDgxwSJgsWOAQIEGFqCK9t5kK9mNAj7evfPgd3t9p1y9wwKLLi/hv8N8ShLsAl3BtHJbppK7ge/OWePX0oqUtZrx1VSWmEpAsuQZ2zepucCtOibhoHGJVxGKgLTSDgcuvfcovJsvxMuIu9IxFou45Rl1OUYkYxwcFZm+kPaaPl8jD6yuwxolRKglTMn95w33/yjlBhN8yGzye8YL5wzgFUkZ5LquXT/wAa406I6A+oYHwKixPI5QcImDBwAlQIEJlhCMlT0JzUfRMonYHtwL0UT+jk+ocDg/ivC5f479tJomFnyMvX6lBa+msq+2Wgr0hxhCrshn0CD0wIAaCmsZuT7xlVgGcTpCrKZyGzzZycjVNoMVQQNTHi7rWsOY3I1sxHkzwSJEiYkyxOskvUvmmn/iofDI5IYOAKiiDP0PsjCZovNVG6zmINy+tZCN01cz/xGILXDlBUbOg5x3S7Dn1lRJkiwCyMJgZYKhBCVwvQiMGQbM5A19Z5oe3B0pf0kPviAgiRI/45BbcM90SZJk2HoYBctRpftMl2ab7fw53HWuiLnMRV0vqzPRqOWcbUivDhGXOkhcFZ9RbzFtWwwxyi2jXfiXHJIqsFgBnCaWWZyufwiXSVj5b+WAdn8I/WIwXYH2/ZC685bGbULoE8iJCZS0Qjm3qZ7Kd/tHfYfEqzCZaQRYOx2yz9OZwUKwGPSlyAZUIgMzkR1SaR/wDDTm8D8wBCghBWQRU8uSN41wiooIkGsWLgBhSOUXAiw+AviUQkrrWvl+IQu2dA14MutIeZ5Pu7b9wwRIn+RaZV2UVekz0xyVvZDabrqbA3c5ey3WrUDJqm0imZvm/w7r45MtMoCc8zaVnKyxYGvbD2b+7nGUGa84gl6pTYghaOnaXglzOMAXAdFzPkv92jt5L7TKWHkMfu95kzG79rNj0S1sPh8TXh5jGb8xTX14zxGKsvUmleVhoBXWE0eoaicmC+J+k9Mcg/XCWOiPaONZ6VwB6RkuAy8KvF9PtMUBG87iUR0lryeNkSv/Bzg/n1cocYByi1M82bqJkjDSGoIwwlRjEiQJkjwgjqDmHp0mTKAehHCpdRsvrvevqFz07WR8QwwRIx4q/KJUNs0rIlqCoDxryhSwXSNHJI1tcU2DpvzZm/Rrz9IZR/z6/hvDX2GuaEzz2Dm5Tbm1RKJcNiX0WGU7Co0ETsPWXHezgcARqxzMvMvS39OrM8KahR66/EsXK3/tn86NsRdzr5X5nOnu578UfB8gPsgQ+jCfcSmx9ZP97Q6LspXvNCqs5lwLVMcn9yGkechfaLFwdkuzMkqesOm5GZIpz7QIN0cQQVzNrd5JR0bgwLSXJzgJXpvjdoQpGRWMWkUzDd/wCAVOep5EpaUYbzQdKzXO4u9JTtAgYCRIImCRMVjLgwYMdzZYfkpr0uXtLg4XsOczGf3IZQ8zUXP1L3hhgiRIkrCuCpUr8QBci1zHdPZ4XglzTpKQyiuSer8BvG05sh3IAEE3Vy/M3TZ5wkXl32fwISMjrv0iHWQlPVAHL2/qZvecllCTNJu8kc4p6rCibLRvSLU9mwlDY+9szxK3hXSo1Pc7z3zgD02PEu19hRBylXCnTV6E+F4JkR7pZ71tIhU0ZbbcvMz1h2YWlrqP7lFwWJBUGcLR3usb9rqU0zdBLKtsTSPpl5XNtKQVl1ZHKClLC8maZbvHvO5CueXQViQ3DnLNKd9ZrSHfK2nzNdYYdM/wBwUqa30Dmest5RGubLTgHmSn8esu1SvvLR7DCAmkDmBAofcH+cDFrSUq/tlCFduyA5xBztTp0i8oGdIc9QWxlhlgRIkqVg8IxyoR7KZq3H7mBLmQFhqO83asvyyr5h02ejJcjn1iEdcPyO5KYIkSJExqVKwq5UfwEDwy1nN1ZlO72+IwaYh2in3ihFIEZr6OTvAA2QAwZmxFMtf6+NlDAOS1F5vJMg7tpdRakMmRzJp6QtjBu75NozXZIykuRmYTOhtNLV6/qgZSXqdrn3Y+8l5Z/uLwPdaINYl1oDe5n789DvN3l8xlcq+uCw8weFJnfI+qnOYPP/ACMM4413tnZ4Q3/tzZndU9FR8CDchT3ufxvSekkUeKK82ZI847QRYZ5S3CVdIj9xhatOjFddotypp98FRz0C98s/K8OxVawrOTDOO95r6hFIlI/5vIYF3gkDIhKtCVDNy5RQZ4MXXwRVua2E0jbbEhElYBg8YXnXWZYznoa+TOdMMeZs8CwWyIczHc0fueuSh/jKXgP4HM6OEIkSJKlSoFdQ2tfpBUNaY5LdRZdN0Q5YoGTrJ1/ZMq+tzutpGKlSuEUgUBkc1UZdCH5JXMoG88j1je8z2zDs5IA0DsESLK0qWQ6xpU5k4BACrICahA0rpBsr7nImjmziZIANokHJeBEl5VqMy4alRrDdHPugMblyv0PeWMFEgQMlf0zlKTYdiDN4b0XyfaD3G8tuh71lyuavgp0X1vsmDgBCx7h9J2OHtDdaB5/qE8wv3jMfMY4Fr4nv9JH1UnvnKJH8G0Lfno5poRuXthEuBl3I/wBb/CaBgPnaZP8Ax/GEVSsNafEzMUjhoO83wwSQ1K7pqmwfZBisZsoRZ3DdbxFzjT9/5mmJzfIh5gCqMHy7lrAAABllgQy6TQtzRHDNnQxnLKTEHGisCROEw5J3aGn6lkqzx6DbiN558uwma33jjEEsggzGGCJgEOSV6bdWaXOaWwXPfaXZipVjdmpuJ3U0xqV59BCgh1GbZcqHnLFCqi0Aj+ojCROGr8nubkrheVD2lT3iVj6NPb55RTQcnSI+jTK6oDtCneXhQ3KzyQaHMcqYCt94QYGgzXKZf6il+e7A7dIuDE4ElM0OV3bBqO+kssLoLXkCNaxZVUyr/UZUH/a/1OdT0wP3Lf8Ad0P0e8SunSLhlNfyz9zpZ7RPlXIXM9zN+z4WvGIXE/3EVexFrxOShHbM+Y1/LXlVBtHT4R5YeVKPVZZ2x9Td82laa/Ihocq+5c0SAN0geqoWv4wTB+46oOBvpMKY6i+SZaZG+UoHJUIRARBKpnXyvmf5SCFrSDvsrC5gq+sz5vs5xNCWqpQ0i7Y3KZqyUNTJwnpjipxJElYmHr/3tdPzNHlfAwG6UuJHOJ9nxMoXLJt/v6ljgYa+KRdFzamWHaij6Q9L7lcovaVLVfB2jnhcHpxFruS38xAFdZs5Ytird+p5MAdl+o4RInEmBnMiGfcXuOsyin1H2G8GXEClBmsyTbX6IkSG98yc3nFiB0KKJcqMYkY4IBgH6Rj7Mzd0ddTU6TmaUWs1DnuQthe+n9zzG2v0C/pNLddmhb7D8sFatvL/ANlfmXeHVU/CH/J3MsfIc/13JDOVPciCVfcS06GXI097lzTUQ8TmNb22ZvN8TU6X8zRTZnsj4LZWjr8bSUXZ14LPS72hnVAua5S2F34HytFpOvA6IwIFz078pbzE6QwLuwygunlo5RUMU1zryiMtT/JUesXOEI0IbpkQQG0+55jsYqmRp1skKXP0liJmS5al7pMqdGdKMNIqjuMSJHgML5jUznKN961TA8qNz5eJwKtf6itGsOVp7x2iyhyZRFRQyoW+k1Ucsupb1+IGHzQHKRRfAS5aCtv/AKEy9aZ698rmYMrDahEwThYHQNV+WZIpvNDJtkvtNQQuAHK6BJoPuX6lI5sc7IGMPsJrLP71mpx5L0OTLjgxxY4Vo5w6n+oPqa5AHMKyvr7SukSzPnH7ZlS316f3PmBFyY+lWmytR9B8syqgbYRup+g9ZewEDSOzh/WpP6nXUe8ZyFde7XlMTf15szCoHt6+zHFb+HALe8X7kDaP3J2mvmfJnqv0+syqs3/3DzAts0dfM0qX7TL6mclSrz5e9vxNfp53u/3PBnMRp9MNE1YIQ+P5ofP8cWRNkPPK88oss0X/AOfRKcpHftAk7wUdGMmcvB/yNzVm8iUkAMAAebt5SppkG0cEnTRHeG6uCLiTSC5N4143aRa0im06OA3wZwxIkSJElYEUqZZd7fJMpoNi2s5nSunFcWRd85IZDJeb9vmNoX8dBmHMdOs2beFXVdo68RCDs52RBgmbWHmnftnOarWWGu81dpnQ8RiRjg4iO8l/yInXkZB4jb+b+5dwQY4ezCaS7ly4xjHBiwzg1K1yPUa9V1O0b/XCq/aVwAfTHeFtuKNAye1Q7pNjyak2odin0mywV3+UxXNvJ5Ob2H5Za1bRWmy+/MW4/wDQKitub+8M58P6LyelzMjyBuavo+0Y0P8AhA/VNR1+tgRjn19boTMQ+ZpmhmdB9xqBZDNnV19NJZNOyJdWl72foZ/2FGZ8nC30/Ar8zN21Z5H1v0l3gFxuMmM9xgIqenyQfz7Ta5aWXT16JqXvn+0M0hgF5VREsOh9BhbMw3RqaFd/8fs6ehBU2DpqFdsyWc2LEgvnFG2K0gxjtFvIjqHZMQRZll7M7DbRgRInAMUNHV3hV7GZZa3tOkXFZjXrbJjPdveNvaWQJlIKTxCFsDMBsuuqwE07fyJ0wqBx2l7wV9uUGf8AnjTmGV9d5kctkANu8bo7V06rgPWP4SZSAQVVMv35FVcTnIC1RrIH8yix7Rly8XBY4rMxAomdk0C9bI3ciNZzrZXRlNeK95Z+Kgo0S8t4eWOtFOX0fEz5L/eJSJqcq6edTMFdPpQJk/8AXOZB0yBd+OZk92aYvJh2SBCyt9xzDtXP9E/3BH/dJlLLMyb+yZvNfXKAOlLGs39Ih8xcYFnQ71UySKU7kNhzPomZK+/Q8lnxMxV7XWaSIoZKh/tHvDhY/r/YUeZ0uHlXkeGfgmarfwEp1lRYstC5vPAGFndhujyn7T/eJOPN6xJUqrOzL+rkgJj8K6G0zCveIBHRlPZez/GzgPSYB2QYVq87vjrqRucLVnKUYfTBi6Qdpzl9ZegwiL5QqgqKa0zJnYLgBBEiRwMBt6+Fn5m5hF7zM/AymaP0Mr70kZLbq3ylN8l1B16sYqKkFJ4wMAOfAS+EmpsCbmsvKDRqE0F0Out7doKANsokTi2Iejftg0Vzll8zOeQ5aB1uJckTtXC9CkQLoC+RVypVS4uFVLjng8DGMdKbYBs//F8wZQn4lNr8aTRR5KUazmE3HKVLy+NJfeWVdyp+IWSgzd1T7anqoXnwPsy7G+Q6PR0vX1l3JYjkPtlXfrzXp6/Br/x97webekpBH7/6L9JoyWVcj6oZRZrOoV5kzIW9xXoQ6tIraBXpF+Ymv+JcpQMNPN4XdmkruX9AIY0Vvv334TWES+X0FHiGU1TOGTpMNOUWoS6GBSWazJHN5fKLk0AyAMg5GCX1WaJY637PhLalJdq5rEvl80APuRAMaMMY2VTNvJyen+IMg1Z/AAZSpvD5CI3MWeEKSx1l280ZTWc56VIU9ZbhJcMACZp0VYjjVUMMSJHEwL3mfRT16+Q+IhCnoT/vjzqkA7ootDYOZp5nSpjfv9zKReQ0Hkd4/rXnmj5jiU7MrhMChDmQ3cydEy6C2NaX4WCPFR0art2juYNtD2ZrgvA4XheLGOF4MGZHtF2n8B5lMg/QTf8ADnOqhMAW/XLzNgr62DPsPnpA1zhUc1XszJKEA59Xe7mwOpCdEYOma5EmVrO5Az+5l6yiNBXZszJrk10ulRp+qP6VanT59YzxFXKD9jUyuMtjYNY02R09IO7OsyG5RqLNj1NpbyQ7tCIxaZduj0iiQ5a7z5RRWfDu5T21yEPkymfBkhpt8OXpHBmyL57mIs92AYzwGAJrMhCQ2rXdiuLVuQGdroTKK0ZB6eU6wsXITYT5QZ3AXaPKuyE/36z/ABMxbMe8IjaZF8pfJs+cUPVgZ+Bzc68ytZa6ykwq7hgSFx2Y2yDQkqZTBDBEiRMSKC3aFXfm8TLE3JPLzEock24RwfRel9XtnAhovRer+50gCBrGfI8zIPTZc/gvG8ViaQdHqNDAz+egDp8e+FjwsqZWZDn36QQGq3pjwOK4KGrURpcXgeBcqZlZC93T+/8AcyFzB7hefqXuHfYfhn4uDk50Fs14o59ai3BNGyWe76T05Xz6OfaKqIWau00se5XuzOruuTPW/UiZ3R+4171JVyhQerJoQ8rGdunQ5zL4wH0NHrLufUQZDkzb5xWfmQtlsvV1YzDp9BP9YMRnRJ6Dcrrzl+zxb1Z8g2byD7vmXWddbwqUHdNFZWfaBlAiwFoFqyjyd3Y59UvU2rvOgj0uskauJp73R0mclEe5HQlwZdURRvb/AKQBG8Ia5vmej/hMQGbMlPkIMGykcy2u8JZMFRWC96zMlprEuZhnMpnDy0Cac5ec5lco8soDWZBAl5yhbYIYIkESVgYOXcyM/fy7S/7Sn81icZBjSLe5ufbmQOAO3o6XyPqKkyQdnhuXLxYwOfQPeZj8V6F3rpXpFhjxrVPLOFfC5uWN4McUH9Sgix15S9ZeF4XFimZ8tc71Jo2G60+3PwkfmizTkffmD0nDmhn4bdO0sBBtQlL0z8Q0FfRpzebr0nVovsmjSn6hv0AzN+O91eXQOcvT70vyPjDoED5gU80OgU2JXs7H+5uAYMzP3R9wQ1w/g/7mbLvt7SQ9lD9YhXubNofPeV3+0YGod7lbec1rzufnKbRfQTIxGqZpoe52qBkFltmAecplvLU0FseCoM+PqGeXfiaEdlsj5eUvI0q5fTkPmZaiiKiZA1eQbzU/c1l1dXrM/wCuxLWeMUKgDu7s1SlcRbu8Lb5gN4n+Ct2Z73gCItF5RkG2d4UjOdhFMrcGzGr2c3OYjboyNwlLKndhRv0g5M1ZmPBwkETAIYvOwjD57/c/r8Fxjk3Qfw7kG01TINhst9JmE/4v0ekTBOG4szrNnTZm9oTGgg7XFjxsAGe0Xo5RYuF4bwwWLUu/GCYEmkYkcNYfXFf2Hf1hvKbenQ86eZlXPJk4+1JMxn2dNFO9xvcPbd3JmpRnWz8s0LUJ/rvgPeVi38bETq2D0Xn8Y3sufL6+kdZK6OrXsuk6cKyvk19IjF2F+BuuYx6dUPMfRiAHaYzy/wDibXNGn38Zc2Fu7yg+8k/EfVmB5PNfMCnB0w6Fy5bp3t1tzL0mcOebXP7lnwSyQVOsciJG8r4J/RtJ1AWmebKHX1F+p0whU6A4ufU8iZwroGjyCAVryS8lri2bQ6rEbb3sUc89IMzeEoXF5p8H+AwAZsys+QhCupjuc13xSCBxhw1GEU3mZF3YBmmXzhZ4AsNd4gJcNMtuUYRxCRMKwUUc7pSH9jPeKt5pX4ENKHKOCHm5rc16TYqaB6JWSd2vPXbGuBmTLMYnxLWdXp2z0ZkaooZGQZRZXGS2d5y2fEHrRdRwXguLGb4MrD0FGWThY8C02Skahu+pYnK955Dk79Y91G7hYpmYeWGX0ZfuZ7Pcgmh7bwfIPbafFI5QZ5UlxcuqnLaVtFYCWS28V8Yt1veZ6YEvgC9A9WZMqleTJ2Y5+bqa3EhlddMvuaSWWdbVljLefeIDb05Ezh9elrR6h5Y1JqC0FPYgmfCJlOxn8E3GFrG1k94elzZRCb/+Ce7pNTtGA7n4xXM5rjlaH8ZRsmjpAww2sU7lQupKlNzn2j+fL7dQwNpkWx3F5fmXLlwYQMcJcBQ4k65142IZpzEYFRTq8YzTLkKAWwa44WPAYFUDTxD2fqUR5SslMrOTowDu1LaxOEcLwDVnqCPIBT8we8sByzFrUcrIlKOej1OBbSy0OYdAN5ojf9o/YPM5aZ8KfmMWosJeKbhR3h8jNxQ9mOo0ZZI8KX+b4i3HgWXUX2l8DFlVUtdOUVVqq85eWl7gYGQvJEiRjKYs8kGDmg65k+X7J0TyxZPD8VNdCQQZ267j0nVrZyqQc89ZSsYN0MrqLgyXQ9GfT6zpoTl2Nr6StQ7yPQY5aHi5jPujmV+iZE/eV3+mb5UK1/5cHRz3fL94o51AWryCaC4PYN/DtAvg7zfQlkBPiIbU8mh+omyhRbxUS3Zgmidj/SIykwNQOnadP3nOCzuftuAQ/cd5e/Ry4BjibpnO0ExowY7EqpeNtdT8xOfsPrALXTXdEVS1neBZcGDFgEMk9aEmEzIZIZ5Vgs3x/vmaOIhiRImI4h1c/i/2qJspXs1PJ8QVL5xq2/4FnXr9iVs6/mdbHmDf7EAntNr94fv2E8zQcGbghdxXtFa4XAp2VldMV1wyindrvO5YvpmZ8iWe88fAV7y5LuXHgMtJRrgZcfmXXBqC6FhBIFbkcLm1aFOWF7c9obnPKQ9rMGOGiNvTh9T1h0H6E2yaYq0/3B8S4VdW0p6s2eodxsdrm3m/uvyfKbU1SkdTy/aTIMKmx/de0J68J/KcuqVRHzXRt6mb9M8dymhFc8pxEmSVYeQPb4mbyGq5cb9XpNVOl1P4y2mybX+G87tmh/YIT3O/x0iYZ/N8YBlOxT8QZV4VuoE5T8tO7yOkEcq3nKXzxBVGa7R2T03fEKWt3jMe0DT2gZ68soKJNur8xSq7X2QRiqJdS7e93RYpcuDgMMBeFg4JykcFYcEiG8dwxIxIkqEIRKKaTO4OeMo9T1+4OrS8v5v69JUt3xND0r8NdgxYXFIQZibQ8jWlPdKF6bd/zLGaoGf/AB1mn5HNby8me0ODMo8BiSMrOVG2qPTgYMyRe8vFahN4vO/1K4zW37QmyNM0GbueRDAW5i41b6NnxNDCm2DAtioDfTrlmS4St4X7QIEbHPCosUUmS89pvDfqQOp+d5jqgc9H4zU6NzOsImmeU1g8rp27Z687ICyOAyGi71cFIXiGkDQqVC131hRSEm9oNDeWRsmRe3NbmaTyOAHo7ktbSjWrkRq4rGb2MKnUW9N0dYQ9l4naxP6UhM8w220zusd1q/N+yOC/+GkEOWdpfMiqhZAavIN4Q1+1ZHV5sGyUEXKZcuJSwQT2kdH7cZBjnKwvELFxCGxNRZp/LRmrJ2QKjd5Scm7EUWXBgwcChCwcDIl8JTh5sy8BllggvFuJBExIQ3BRzmXt3be/9tKX6gAlidQ30/BUr+IJUtr/AKSHBtcxBS3Tc5kyOiE29Ru7yYRdC9APw1guoQZImkESMv6L9YZxesvFYt8DRg3FstTM7TTg3tOomTKWR5lWM1azuOcCUFobMyd3L74LLgCIrJTnziRmvMDS4ydnQZywCWO8WLESyAeYof3KedAQNB7fEqhPTXZzMtdpaPYVtQYZr3H4g6ETuo18TS+YjlAyre1s6PMfqdHFXfv0XV6QnZa3qRoFQj3SOUHLUp6R/wCE6zIoIORyL0ObvLTbcvNzRP60yfcqryjjqt6NwtiRvbzfz0n+oOPolnR96gVhUEwioy+VnTMjmuxEWywZDyDaILfSM56cscgGZ0j3iy8et/IdPwEIpmBrOSrgoby5/cfyaYSgwtIwurmr7IsWXgMGDgMGXBgyowDBxzjTlwW2CxwDDsiXEiQIGA0yk9QfiWgzerH+5V+voqO23V4KxuNGgju+RkNYTvtwuUowDcLo6huobC5kUDKqz6WbdxE5dD7XH3kj5o2YcHW76MFxuXiuDgGSgLzmeZ1eIubzU5ek9XT7Rx17TaD8rW7yOA6Uh4dDAlzkhydIb1hlTXL3ngcgll7B8zKBvNoZXyldjILJNvR36zUuBd28vwTQRW5dGdsok6Z/JRllFfLqsgMoOFXZOp7wtWjVz6eJu8Bsh5rtM8ysi6AbB6xjCocm9rJ7PNjmO5Mh169G4elS7dFbIZ59poMI5MxlMjMomtEA0vWVgKHP5U0HaByEPSAc+SMtzG0vymma5DdnU3Jq81u8NcFYDEiNTOAk5pT3wqNtvuP49dml3lWG0BVtGsrv4CLF4BgwYMHFRS4MHEsMkkqKXFFly8S5rAwDD/NjlMgLDc1H5hyf7qbcT9skBvKgieby8puks09aEuDjWFrfcLp8iJj4U2Zc8paQgOX8tGOWKkFK5JNMOq59CAEBQyAwrWLUusGXWLGcwNs8yfrE3E9HFc+BkUm7gZWhvOSRo+zBYRRz2TUlV6hdTIJvNGMIDzOsCGq6d7NvkhCgejkDED6OuT8F11JRBHjJny9Sb5cu283YIDpnuQ1TSSTnbyhbXHUdNyH/ACF/1wkrdzc7zacqbXu7nwl8x3+ptOtdbJ+nrMxMnS5k/UrerUzH95E0LTP3yH1FaBKaRtTFn+eRGOGcunzQ5EqbbOFdWBOaiaXVeXVBiGhlh5Bx1whgulvZK1hHS8otXR6D8QShm7+sKma9WYsXgvG4MGXBgxYDFFgWLBhCiy4suXLlwZcIGXCsI9dq9SCpyR7yn8g3mlPLXrFW3Vwvx2xMsza7DoToJBt5fgSLnFd3mfrWIt0hrQmNO9sPechAK29mUWLivBcuUpEyW3+0qsXhDSjXRpM6WbDm+IlTpuYLCagbkcZbm3R5LU2qoZxi5NBpoimeddo4G15Qz1G1R5exDN1y7omiZXcyHumZHVKAOb5GAqsyM9ou1H3Ky06av0XABir5r6ZHxOcmnvtfnFVm1QD68ztMtC7745dUQl5am1TIFdPkb1zdpeqexbis0+JnybHPLcec3Kg6BLf3zyX7T6TM7u8FlfMYxYr6v2QV4lGUMCDYCrKiJDdVz6nP/wBI69bYVwVgEqVKlYBDDdlTUFXMuCic5RLX8U52wQ1lUVhZbJ80WLxDBg4DBgxQYMGDLl4BwFFwuLLl4XBl4BgxTLW8u1+peaFfvnFf20zNjTY3hlprxJzrgrH1voN5ko83WMgyGTk5x21Wq8RgwhK2a6KuZ5Irk8lSua8PiWWvvO0jmy6Z7S4m5j3/ANJZknnO2xy3jgtRYZ4Ibaea8mK1EaE0WVceYWuS+AImm5uFFm7PwDHLXsi1WnW4hBGxzuKEGwQznsNpktMLN2VFmY2cOdOZ8zKlp3Ida9Lnvw3sh8erPtWD188rl802szkv3GNYyKs6dCiGic0KJo+0T0Od+/K5su5VAS75f5nD0Urhcq/ma5mgHouXaaBRQMgcg2j2V6u19GUjrycoFb94O4pKyozel+sP6b4GJHFiQ7/yJyWCrJ5MRzgXNM+sNirZ7pcz/wARVK5rqwwSVAgSpUqVKlSoGM2A55JcpQy+ggp/DYZkUd2UA5Q02gzm+M12ixfw3iQYMGXBgxZeCixZeK8A4EUGOOjlX1mnrqPV/SK2KkFI8mVGexPH+5ak7sp+5nKyVFX0Ezp19r1ZfZtrK/EMfQKxzJnqQTbRQqKXog/76TNBWbeV/WkE8uXMfoS8bnqtHJ6InDcojNApz64Fxaj8BCND22S5lc6fbBac8nbhAVDzhCeM4ll8JouEHzReXM0EMjyS5YGj2YrmNSWql6ZQ+gxj5+8uf6ZlrlQ0VoPV9pTz/IF/PSeCUMt+2VLaE21nsLvfLpNs6pb6vPozppZeY595l0j3f6uQ/aUM4lup3viZ82oGX6T3lhKOgeRsy/RKPWfb7IFvbiTt1GVmAVgn8E2/RyAZ+j4nUU9yB7IvA3cjT4J/OQ7Sn8x7iZRbq2zyVVrpgQhKwqBKlQIcRZRFuOWGgxax0FU67fgJbpg8/Nr5ixYv4LwuXBxHG8bg4LLl8Fy5cIQgxxTIhIOQp+ieMGvVzjcHoH2nJIrW++8Vs6OmekH7w0vXl2gjLJyf7Sc8afwjiMue3kASZy3vm/36xajZ5gZn9JKnODcH0r+yXznPrElQIkqBb0s8+yq8+6JBBmpTtBl8V+ItmfOZm7VWznCzInL8FdvZP3EMZcgZVLjnABlKHy9xNgNPWfcOmzNtOfnX0lgOD7cztnOepHByD3c/EZxLJINa7tzUU5kdfk/pNDJAjol07zJlvO6HIdD8S63m83Dky3/ITbzF3/RP6qKHppNtFg7rv05TudIA68k00UY3Gvr7MBzp7tU+JeVi/hIEqJKgQQIQELwvHBU4FhN1wz9ZqVNMwV3fgqTUoI0oCoMtFlmxj+IZcMBhLgwZcvEcFi8V4GI4FUPT9kznP/bptv3GGyHl1REucWQ59JQ92mXZFdr05OsILkzWv+TWnMH4zAnUTy7Q28yoNR6icv8ABz7zO5HujVryX6EqJBi1EGwXEczS5b4v4LitTb1/Cbqo59o3BTsTau11jkJdbMQ+R2c4ymtXFfI5KE6vA+sz0O0duryLCzPDLa23PQ9ZmmMO59DzORYEJNhy51ozfWygVG+t7zJqOEJtexLlcA7yskOo7kfag1P7BtRqyuRqVeDtN5gB2vqJ7MzrdJggOdRa/PUXiGrVE7P/AD8FSsCBEgQIMS3gVQQzYIGUAHPcJTCBOPI3I35lW5TphM0smp2Isf8ABuXjeA4L+MhgNwBea8kmQj5q3sTPS2SyeX9EyA7NFnUbvxF/jJ/7RmCtVg/jcDAZ3X8Mdcr7sEjFQDNma+lwgzKfSIata0K78pk0fKNdg8syFqirO3BPzhcUcm87WRMth+YqC76R6sjmspaXHVlymSK2k3xvA3LM+qqXjzMqMlW8gM56r+Ok7unaX4MBqz4XmQRDVirOxBgb2u3t26feaNQGSvugwEuxoeZgT4RxoO2pmR6uO5ar0Ylav9fJ+oBu2oOs7KwbmPq1MiPGxNew7MAc9b0NAfJxhiECBGAxBYhF2C3KWvSclM7SKQawlCt0D8jX6lGw3LEIyOrcapxtX0MOY5CXPnH8g8Ay5cuXhcuDL4b4yGGFSAEdcyz3lxyzE9eabCN9pMsDn6mvd3l43+QIYHrFTqJe6BBqKHBlsnqnxNH0cnVXsllen5/C76YBKiRivy9VVzWJbCpsTWIFX0i45+3Dc+b/APazKqXk2dhM6yN7/q9Y11bdAPgY7u5Gap0+qD/RbOQyn/4risQ5NCBHvq9/mJ1Ix6HmT9vvRevInvHvRYVNFV1I/o7TZP7chXvvuEfTHqPm+JpbFNhZ14gwrAIECGZ+A+RDPgWAlOGi7zXob6qKKQI8xvbIyrgtJezQ8Umjc33MKnf0kYv5bl4XBxvgIMY/gIQhlkvVurs8zYm5yG15AQ9qbP8AoekRiN4r8zgewhFhezLTyF2N5WAwY7OrbSujNlgV402+U1E71jk/tdsNIkY/muaod3Mje3LZmZ+XKucvjH2hBkeov+O5Nt44v671TICKirl+bq2jbzh2ursG6Nxmrd5U3HfPeZTwXVHrGWy7ajhlleuU5XxmBCBAgQMKwhNuA/KduHamUHO4AyJm0jBlYHSE8wTdaal/djBaA5r9ni1wyoDkRWFaae+wRP8ACGXLg43x3wVgGFv+p9OYmc0s33hylDM0do5fthGcZnw1wDZ1lzjl1hiE76EdTLoRrqQFuU8/BeDsCiZ2MRIToNGY/LnEOJZlPI1y5yqvyO2R9IkSMSJ+S8FrZdIwldZafiIjNYFvliMW+v5DEJUCBiNs14GRyj0q0QNfpGxyYMQgMENtWBK8zQo1BWUnMSEuG/tY8Pk+K5Hc5TOW7R4jH/HvAYvEYhDBArOHXqOYqegH3KtoNR0EACR/IzXa6qhkreggMRI+Qkzl6aoXak2fYT8JImnVRc+/xpT+wiXumaaivg1nnzzn4x9k8An782H1NfDcQYJ5TlKNGWFWvZWXxe85S8c9sVFHQfozwn4Tr+syGhIKV1IkT8ycw1pTT8guFTXiv8gQOANXaW5SuAmyGnc2ZUaBAthiXlLWiJSj5rWasYY660ygQX90QjM0uHoWBCdb1ivPdzixYFy0TgDhOA/wCBAgQYR9gyGV+au0qzBQ3ctlXG1o9yEvNhlH9xHnNCfLZpRPTJqqnXR3ct/fH/cRvC+PuE6WSI+UJm36I67so1s/EyY+RoOfFcuZ047NdSZkGxvBHXHPaJ9kr+NGOArodhvE9Grr0fqP+WNS/KHSuU6r85gQwqW1EQRuH0DG6oSUU3SYFiIV6g90D3TWI5yasZZ7wyl0gtJT9nTzw5/M2YEq5pRCxjFKVHOJHjHgr89QIQSPCQx/p/eGyj16rdl7T6JO0O7HiWhvW9eU+uhgzPdKZmle5TpEQVL/AB7iaI1mef3JyeXNkq87TOPujUbQwI8wh0+90f08ZgMuagXBT/uLizWsOqeZBtD1OQwfyX/lmJDAgwNCXyF7aUmtSlgRMrokW9UBGiWmZmseudTGDCzpkMHXc7YPpwGsodUyM7nXoIkYSoMtB4guDAJWBEwqVhWFcVQIQrFKOUAtfEEO/v8Aw1n8hPebawt+34vyI1u2hUeN5/MUdZtJ2Ai75mKF9rygp+bVK8vOSz2j2u+TLpHlfRA3KvF3In4O0TY9cJrL7x0IlBZbOhLRVn9Jt5jLDxtGZOr7H9jOcjb/AId4zP8AYVAvXKOqFP4GQjSZ3EAGMo6bdjtF/m1a+gIx/Lf+MYmBCECBiOhEDADUMKBV3jS5GkDrFSs3wVEzJfguEsM0tXMw/rC+DqmhC5cCXUXZvlwWOcrAYnCmJCDFxXgRg4Xg4vCGBdPCtc+1v3jvyJ3b+aEeEnNo/cSb8H+4P/rn1osX9IF+0zDDW322LrTv28koh8GGA3slONsQejUrBrdWVKm31jNKdNJ7k0J9nV54QuD13tNdw2qxMhyxBvqS+Emo05HoEuXFlWaHX9HUlHzdj6LtNWbnmml8hLuNai7sjQc/wDF+ROX8rczaPHOadY+uC+K5f+FbB1QV6FxqpOqKBzJ85RUzyPggGd8s09DYXygSU7fvnAQIEOFUhxTUQHJKCXrLBsvBdSVx1jbAcZlYi4wp0uyw0kWYdcX6Fgp9IMot6phcIwZczsaTKo64FoQxUMRjBKiVAiVg8AQQMLoIxe96dN4mDUoyKfDtO9udDtHLB+r3n0G5/wBbHSDupU+t1TK30R9UzHD0j2d52VDZ4iYs6jIbPiKN6N1+pWu3ObwrlQLnKEr3ZvVX9Sbpu1/BUuXLl4XOsSnUrRE5afUnRzb+37mrNZjoPwEuj1Jc/wBE1rv8XRg/yw30Z2X4/wAPXD3pJny4DAVPmy/CaFe59z2lWOqdzs0XdftQeuyj6hTRa5SvModpRylJlVBLgsTXQG3eMFeRmiEIEEMoUwjluGHzl5rGCm/DssZlRRcFwZUcuSdZXDaOpj5hjKUuETnRFlGG2Fow0hqZUXKaoZwWOaVESmBcQwIOBRxYnAgQIFy+Zy5tP4idLUtBsv8AKm/17qls69P4NWGOqz3NB+/wvXSUlzl3LfgxIWZym+oCcwnKNhHekWpXRNER2Zr9xWlyNobuDZ4Cev1aS8L+Wzit70Cpm8rNYJPNb/JEq3Nd2XLwvgWVZdw6Dkkf09auZzW8ZWQF0ETb8PpcOZOlcs0056JP6YOkSun583x/1m7P7bIDWBB11C9WGUF0hO99YA/GYMuGnK3Ba9k9IECGUHEGDhFcAvgaGA8AHAWXLinXamKzBX5k6pHADaZ0wIA+0HImaJcuNILLdIbaImsSBHUc8tJqgMNI1YyEtCFXBYYEEIMBTeubab0ap4tR1GPUN66T3jV/2FnP0gFzv45S/onsJqqNqBM97oJbV87P7QLoQa2YCk91Jmt0ciYWTzMD311lKWOY3JVvzz4RKxpN+GIN86JuB/yI0dQWrc5HJsReC+G4xKDRWkyMvQcGTaFdmNN5aPDWFoI3mxr7z311mtfm+1Nb6uJ8yL9ZtU/jdjT7MPmVkDdp86TPuep29Dw1juPaofljdX9z2ghQFZZYV/gVEO6lWAYOAwYMUWEk4gDFxYsWLBgym8yK4ZmSourHwAhOmXxSyWYwjrfFGysuMB1Zm8YY3kIayjF3Ehzx0m+EuUecG5UtNJRBKiQIGJtnozrHWQeU14anJ2XrPGJ83+6yxD53+kRHuJ9y2y3q++kQtRs3TRpQ5p3EB9OgtesffWHXRu84zpKTbAlzOJ55es+hbkqbpz47lyu07GR3niaRNkzrArvTQjf8JKizWJCD5xgbmWMckZ7STn9oHvPf6fgTTD335n9Hb3mo5/GhKW4m9/kwsCT+mUE6HIFSrnQlBilwrp0Kxi5ltbV7thKjNioC1eUzgzPtftlY3hXFZzgHSXx0Fqg6P7SDBgwcBhgGOJXgLGLwLFixcBgzuRqK8DsQPCdanvDgE0TqRfbFSz6IGpXFdsszNRI60uJXfQwqRZthaJril5zImbFMBgrUNV2Jnjm3S9tctmHsu2j1uD7AAZH0Qxe3Z5+sQXuFfcpjVlmrzb+Jafyb3jwEgVE7Yeko5Ozr5iVws2qSZPlgtrZx2NHWDKjQjhf4FaY5IzcP58+Ef85WeH34w40zGocjLSuk1jZ8POF+wZ6rRd6zKTuQCHUvmAG0o5TL8uexX0N3tcSoMtsVBMt32PL0hgXjdZTSH3Z7FemoT1vxl7kXMPmerfN9W9fpMrA5ovdmpC5fURwa66BduTDfguLRL2e/H7D04DEYMIuDFgUWXLi4rwDLp0x3w1H5p+sKlzQ94xxGGw0kKQomSwJCMGhYREo6okTkzMiI6SOR5SQZcuBC2MZO9BCfEs5PUfogMd3bPLp4l6jtcNZjl5QaEoN6fsuU5bGXLzzhBKjg4LhfGR4SJgEW9yaaL2iV+DLxOsFyLTkHrPZeyUO76ynKBUSAHI7BPtVO3LqV4tiemexBoR/EcBjyWv3RQiFrQlFKvvea9V4XhB0YGrNLWVlPm7OxFXyZA9D0loy+ALw8Mz/NTTcvgQ0HJ6E1RvV3/nGMGDBgxS4osWLisWPA7HIxeDh3EvrGrvsKi5mJHEIYCBEeBLUMsqblfuIUwiItZZy/qJCYyqWIJdMiZ47ewgruvkL7ukaELn8l1ZaV/ffSdSC5oTOwspQRlk9CjN/m8zCxKrXguLF/GmAd090KZl/wSC9IwWmNZCS8w02n8M71ZuQ6Z8E3tc1/c1BOaPlLgkNiKNubFarWb34x/vuAs9i4oV9YkSZFnmuV9ri3KiUohHxvfhEWUlOeGXUhlmdImuDyqvoR9W90yey9YN8ORp8wPibzWMgfK+JzcLNzJnffeL/AFzRM7hc3itFXnuX4uOBCHAMvAYpcXBeBYxl4EVL1J4gwz+5j31fzgsrmPiJwBwXBlM0qeY7dzgAGKJs3hrTcGDJll5aqAhM11GdWT/GbMp5nzy7eJVp1jDZufNetzIrrzOUWOuQA1XIJdClzHu84t4rFi41EqGc93eZ6gkP5jnmz7x/xKn+kOUN6QX2wL498EfE/kc7yn8PfqBqqGwqPZlY8/wAYLw1H1V8e8IaFYXgTbgD3H6mvHm4F3B+yQUcOz4jso+WAcgW8oBpT4oz974RTkhljv2ntE/wuf33PafzA92JZdyPwk3Lud9rjltfVcURoUhsFsGXA6SjnJ+68v5zjlMxgK9X9Dw6JSeRU8/xXHWBMvxJ2XL2OG5ZDSfb9j6cBiMGXLwuDBlxYvC8BF60fpsPQv2x+X84ZS/mUYmAQIkrEYMWXLjOTg5nnCFmYDJXlhlzHZp0CUYNbLd30WIS9yvTeJ6F2UPaaTpnn0J/Dj1TnJBNe42jF3FW/BEBQVbXtlORDAfuDWn3j3nali36l6+ATQQpy3aC+O/Tnwh97Cp5V9M/iedKnxFvzYVXY79QCiHkKnewKlEpy4UimjMxrM7eXsMNK4X6tvsHrBRwLRc5oI+r8Vxk1/GPT9g4XRlc28n1fzlhp8R+R8QpzOHNHX+Z5l8dhZw+pn7EHAwWqlSORdn/VKgtyE8D4fXhdVM778RZew/CNimK6sMGgtdDhURBzmW7eg2fuvEcA4EHC+BeMwO104H+3Jx/j3hNB/cokJogwYtwLwVKhg4EWXBmgQO8LrZ1tO7/YviaSfPvrLjgtzfYRu6JvPRJB6RrS7/vmhobUfeGPPS9NGfXkPE6ZMomcOiLvL+mFfnOyoz+hqkYC5cFO3Tl7AjwMDkibF4A0fH4Mojzl38VwgqorXpNZfxS4aaR7j+vdDLLG5mKmW7KeNn73HiNamXp4syfcOD7iirKOVtNq841cxZQqmnxRn73L4Asck9I90/F/pwRl7s2KiuRGCm7M40lIHQJ7mQSv7xMT8BFl8NYVHEgrtcP6u+Pz/nDP/PTBpG2ARIQjDAv8AOLx99BRc2Ir8V0wJnK6s58f4N/a4d9CLoHCfgylnOWYLSV4L/tPSJM5PuSq+4eNBHY4c/is9IfMePNwNndhp0IOgUcBLCV5D/uwC5lqeCsnw+vDRXfb6Fx8L7sLwU4VM6WaHeLLZ6SO/XhO89js3x7xc5+5Ry+Ju68N7fYj+09I8V41Mqye2b6jaXjcrNrsk/oPCfgOAMFhHgXAhPbcP7uji78nzgP68okTClYLFBgolcNFQZ8RgJzdte0OvCcQzOWo5P7ygVw2ETzgwSoA3yS7y3Wp9y08E+El5/Kc6n8wr3imT9b4BPa/R7wBR2n96mXvOS/CPVHuq94zW59Qe6Qus9eGwXsZ/b+DP4Wd9PcwKVw2E9AjXz7Iwm7ag6ysPQXMM3hVTlJVdrl295miti/zgGZeU9i56dK+mfUvsWZ70g+hP78nSp/TL3hwU0GHgwvBynIZxjXShpufg8RJo2E/VZ+3unbFdI8yXd2GfmWGv3wPk/CfEuenn+f4rgWZgc5qUE+uQ9h/AfgJcYwYsqVGJgEOneGj04L+XJx8W/KEu5QxcauCoEJuRUl3BqEXaOXAMBIECEvr9lf9mS9UvF4P4RzmWAj239OAi1B4atSKz35zlpy+imVsOR8CZmn1I7DjTAmkYvVH3fbhchlwvug8ZNDZr0M/d9sDFZaa/M3N/OWGfB4r+4PWaOBjaZd8KOWPmKc5U3j0y8XzjnAbbH6/ry/1F6m1hLszH51dP5zg1jcsV0dq8prLFdry9vwizdO6Xn7XEq0YOhFly5czZtpQNgvTPr8JhcEEI4C4MEqDLjgOAQ2fWGu0weX/AHLFZTqhO0BixyVeBQRIc4QwSRi82gNzOJeKnJ9+TJ+ZSnSN9TL9cGnAgLXxCVsC3w1mvQ9S1ChD0RV+ODNr8AvP2uH1Qj6BwsnTHwRPbWvVb+/w58A5xaQQLyvnhcvRKuRPrO+8uMlwlM/2uZg4VGRobLyia+8GuWF/e7f6B6wYtSrcwqTsjhFuctzly8VhggWhq31euLqvlzWaQS7MXVZXnoDoVFly4xkCGd3/AHMcuOpUsRJcdPLnUd16cMayEpzayjJzJtesPxEUWXLwIuLFvAsRpgMIMo/MnvBQ6GGR11xu5BYdpU+8cblwYOBYNRWhVCuVTOKpSUwEEUG5+RRr7U+JWBFkZgZPR9wSMctZ1UL8h9EzTmsv5ebg49Kp/wDGXzDhurk8nefqWrKwC94l/UHKfzO95Tf1za5/C97EMFnav5Y/qY0NAac0jljl9Z7gewYAQKDI4bOV7Cf3H449QvxV5w2QZQNiINyPMiTnO2KQ15b/ALmCmapoQ8hNewTzg8d4Xg9M+06P5Y94TQ+1/ogtmTS4uu3xUW8+cvCx7J7Zs96liOWUWXgWc/Keyo930iJAjNF5AubjvP8A1KOq5v5We0I/Qz+BV7Sn8efJZm5YovaC4BXVLQcgl1BxCY1ZmTsDloX3fj8qjbAYkTCuBmiJcEGXFOoBgySplX8ZcPlF68HhGDLlx0zKl7GAWUYBECEgTJEOjUPPb9eYr9M7z2S888h9F8EtishzP9Tuqnn0J3cgyH74SZXivl/SuFjZwLktc277RG+iCHxP4nHSUVlN/sz2yJA6MuxhRylHKVGoCndP9p6QxzDKJ6Z7DA4GbH2ehkfcXjpc1r2D7i54rhrAdhij39kSZwJl1Wvue2m0Ki8A3HaH0iQnZABbG1TBly5cL7lyQZCPBunWczwtcI0Oo/45EbK885cvE1CA83mOzEbR/szmvPzovVg9N6EKZZPeLe6yuMcM6HtbvIdWJZr6vLaHb8mstjTAJZK4FQ1DPAtxYtwMAlV7oKOCbRj19u4V3uz2gjx3LilMbQhFTXBgXjBLIclZTV5jLInLwijVsm/SZpxs9OVLPnkPu74LHEAvZm76XC4oZdA/MobxNZOc/wBxOD5PSJgQ/oYg+H1jJ5BHm+0ecmTtZnKDO5+zxkrxpsmz+1gwZcuZz0zmfgC71b+coZyzWekX2EzvTKXLgwYmtkdNMvm5ZyYVMlbLLGU1hj2h++sWpcvAws9BGQZmYNX+TzI8AQRam+ghLly5cX8gYWb+TkOsCXZmar+u34qwCEZJUU8JG4NS4qJLQwwpLF5CzJgAXlGNM5wrJTuc6np7xI/gJcuEGRvB4AwR2kC7HJ1ZQoiBkFF1o2Tn1m4ryP4owXh5hBT+cvmBQHDYapE9yd+V5MvHQJ1Z1EtzmsqNobWamPj1yMSY2sgOc3Bu7hn74XLlzJ2QuwXEitZ5eM+IVCA7nmfMusFwZeynp6BnNeyflcIOqSvt+yMzfPBcGZFM1Ab3/YxIMvWvTY7rox08jS67qgxZcuDUshj1NhJnDlY9mIYjOYeOwV8MyZbkX8LxDU02hzjN1Rve2bd2b5LPZHVbv47wCXwETLLis0S0dmwTcSosN4Fo5UhqBlPAnHpOxFcBand9EiYBE4a4KgQIIQLweY1Lod10IG+Rm2u6p1pMbuU0xDm9F5wXhC4eZX6v6VhPSjzaim6W5y4y8LuDfontwn2lz2QHwl/lfKB6+FB8w9pUusq6CJgMuW39Zcw9ZcuRlLly5cS2pPJZ+wx43KNKuTmmCq1lAS5eJe6vNX9Lhm8G5kzy8vhLly8DWVcXp/E+7szwaUi5Lkep8TIKYhYjvFjgMK49pJ63m6MY2/XEcGXizaK/46MTqF+0Dg0l43AdAvYl2nowfl7sNYnzDeHgmQibya9T/wBrKENbm97Q8EzraXDy4jgvEMRimaAxJsMWLSIIhCREQDTBYEUnawQUZ0KjPy7F+cenxi8OZnNFVUYkS40g4VCVExCBCDCEnTgmF+Eea2jvL+YZ9Bygyy1l085njmSSQk6x58Zaaj/HK5VehAciXLl4CYG8SJMbCzXO5Z/wDznyB/hGMvl/mJ59gfHBpHvvjPEuK8DNyDPVz+EWl58AZ+YvNn3DlSux5jtNdaPXAyJofOvvZo5d99iNyfuMfMy5L+l+h0ly5cuFiNXKaZXJic4Ed92lUyKBl0Cj4ly5cWPXnkL+lx4DKa2jSfMeTtK06OjBwWXcViZm82Qs2ZcuiJWNzLLhprkczpFgblB3ecNofCp1vXPpjOaLr8S9yz+a5aesPhPczXzHPJw++YPgIUnUf2JZ51yn5nx7PjNweBK1+o4J+Gp5nt2Cz/s4lqcnrBi3KgjQ7kAmshZkNkMIuBNIhmOAIjMr5mcHWtZYseiYYB+4Y6TrqQJVRzJvKw1UuOAQiyCl20d0L5Bb6RDwR/VmSJ6t+hBi/k/7Jv4uOR36w65ET27mdSwtBHDO+lH+4MCuiuPpaB/nL5ljrTSLhcGaCau6sveMLbqdX8JExCW8VZ/RyGC5cvC2o1zH7Xywpx6ilWMaP5ERfX1Yjqr3cBiwcLlwMaLNgmTV4b+iDMzBb3MvcnpOw8oMuXFdN3KX41D6Gj5fTiYQojqStmgc20fuGj67l/8AtFzB83KbXvI/c1BdPqEX7oT8y5FJsc2mzwXU69QVMKjm5D0ZkVGypZyoe096DYurPvnF6EuGDi4EqVPbuFmsTymmd8CackO9sTN5PBPlJWaR3FZ8WyZGPsEX+qdioM3iTrebaK7KthKhlhCEINMqzGoZZOC8U3inEinYSwluzye6OOolj6SEUj/rECMqLFwoVNUqBCSLJo80C17EBUv/AACdIKL2yDDc6nunKVsLi7I6KTUrCeaWZcrturdXnNwGBm9amzKVNdFvqbCaEo+3ePkXdXxaoSq7f5OVYLLlzkmxv/8AvepfBUpnolAd6FwZhoZc5Onn3XtPY+/uuC2GCmyrjy7Iemf+BjfA8F8WsZaA94Wa+U57dnu4ErxpdkW/D0izPPPAK6Z+IJoviWEgztiWAP71fueO/wD29h6JuTIZ09CgYe0Rt/rMdzN6y60iuBxkWBElYe01GpbzlNA80RUuyxrKyVMtqwC1hPJnm9MoKR2PQgldrENG+wTPn+GoFS4GgF1P9X5lwc4Yzhw4BvI7zjIEpPYJ0mKhM3uHztbTCrtS3pgTAzUDccIw98OuPnRkex3Zm1Tmu8pdK5ej1gyPDPdEn3V7stuzfuQheVAq2zyc1eXtfM1OFstq580u5ZwfB+4SJ3np6H4BrtZr1PS4SSioOhFiy5co2QbiS/mfKfMbPmVVwiq9ffGfIE+57O1M3i9dnzfr3iKhjYSWfVHV+ZiVNJPywZe8Ru3tesMoP7jCFweB/CJ0FeRNcDlUdzG9fya5HL3MXGzITrZyNdw8TLu7r84E5Fp/72J87f5T2VCiil9yP5qwqVxMcOgjeh7GnS43DNafqntbCewgCC9MZqtJeoHc/wBcKV9hIrV38BtK/wADRMv9sYcYuHHWJMkNLSgwrpaRHDm9oGay3tyojO4RiwltDbGVBbgsLegWrkQfXEufc5x4UPQ7s6kzKf3Lv2O8E7L0uXpFizfLnuVjTa2dICrnNbEzk8AXw/z1r8HzLRtpgy8LSBdSjnYHvHUL3US+sr8CS8dfxA5HrLuDU1kHR3nKu76nuM54XxLgaZvENF5pzQ7sE1LsSt6NgPbWFEmX9Kyjs/WeC5cTDOXjX42OKqt2woD2TxZvLD4/hPvPZmI7Xq70+OF93t+vSbZHp+x0jDCogkDNYVSG7n9QmUvv68z3kJXN4K/KkFrkO+SVx3jdKMQwMmtqGiHFD6Zk6WWU5XfLi4OLZhdAFWgM1hCwZmh5ftPT8/j9p2LV/dTsYjLsEWXhncvlNvIUtBsGABy1OvEF6RmirhqTSqT170e1SsHgqVxpKi1DyRZf/RXV/QQjg6JE8+jvX89jsyhGuYjZ6kec+YJsPMN9HbOUbE9coFdZy01xuS/GWAE53yzLog+4932J/Ia64RIts85slnTbrb+B4Oie7Gz6X1Nf7DJ7Kl4FaOq6yag+Rm9Jq56CnGrlaTUD5mSDg4UxsHjgt5uqaDmykG6dcYYZsQlWRFYHq7/6y8B32/TYcFY3K6Reh3ntZk9n6uaL3pGT16Km/wCZfDTDm12NONILjDCkW4FyoIKlFPOJoIS+iMzIW9z4em0zqQRLJkxqLgjEwLILBDrtA/nabLdA36IN6KNdv94zYrVc2Xgv4gYAwXkxnMstM1DB8efsITKWkdMUwrFJUTCsAdAvYhovITUqeYRWj1qhWlMquvaHY+lsuXsDgGovpeyh6aQOvKkpEBrg6fQnuGj7jZgpFDgXBlSsLo/rMz2/bE+7/NtO6z2awYSZrpe5ntviBsy7RLgY1EmVkDJ1f6h90ld+suDLwMj1IkDHLWX6SZxKq7uNK66AE76H1ebwhpW0ZBEyeqdT0x5NjfbeEMKGQcKV3lqnyjjkUS3rPqXJrS9M9yNY2wUGlz+bm/WFSsFlRuUD793hrC8oOJWCiwwJk3pgzzqgVNXCay6PIwr4tTCqJAhFkIsfSXcyp2qCI76wy6eaOoqs1fxhDmRBUdALnJHI/cyl5A/KMUtcNhBd1Zr4i7TKJhUqVKwzaZw0npTkrzD6ZzaJfhHkr6Es/Sl6s0GTzo9ozhrqvqzRg5UfaWHndq95crgOMziQns0JnzCJ9ZxMzPyVdGJNFLyA8wz2c9MOnuxqVK/JcuCUzXxSpnkIe6aCgDxw5yUofv2x9Mn9jmwlGz59XnwmtG00CZ4B03XN/XBYenuMOF9HArqz4SEB062QCeiVP2nM+JqTd7xT3gUzUkC6ohuriGF4JCIyyURJTLcCp95pukqUZzk7cec3VeBNb5EV1VRhkwquRjbsF5Q6pZnPTvP0RLay9z5fwTSeaanu85f4Kmojm0PWFCbzbTQVyTi8lb5Zan5mvq2jVbmudy8FWKqfn+5eDDSp8Q6DuwbmAdoRfZAl8eVe+WFjsmJ7IRUzcLZGJrcTPRLY/grhvCrg95l6HWaMj1LPYCEPYwqeWaTOmx4xlhUbKE+eg6TSsXkHAcVcaQoiGX1oMHDkt2+EfeGcos9/STSTGcJ1ntNpm0b1ep4b/M1n0OEzqbw381+O+IMKl2BbKUGpdDAZmnKz3GVWFTNjxqbdAipecHhmQuRwskFJ/tgdnV+JTheV6peTtGdf7OK2b0HINo8ZCuVw/wBfQy8kZlsvGZf19zT0Jly9ItRYsYqZZfXvhXmUlJCWdQb6T0qiv0lGN875ZfzXrV7E8yIX1Y05jtcPQiq2a55xV/AHme/KzaP1pNX7antEXG58vdorNB76cCamvmkU5Qf0RleDUY5Jc5T8xzlwwqaYzQoM1bcNYTMfPod4cA9A0DC5rKlcFYXhfF0EHlzgSxTUH5YPDkrOg+P9wIh6aH/WHpWtAGJgYY7SZhW7/V4Rb1Z0BR778Tc1mwq+cfpegMD78k+HlJ81oz3LFPciH8Y4kEtlEyYMWDgGOVhmd3iCgQnnJGMq6rxuFqTCGbG0SxJpJpnhjLmLB/ukpQbDfoiGxa7nU9ekzBao2v4skISKpImup6unSZg9dxHCLLxu8F6Dfq0J5EmvqxlbmF9hlLinhE6D4J7vbNGUPY5J7lgz2Bsw8jUX5x7UIgGQUdIl8NcBy67OZiM2weIABoy4bgtB8dcEELWVSkPM81xIbAOaz3ZRNcPcjUnYpPYOSe5aM1RJdEegIBYFQc64UBXQzic8HxAljmV5HgJqICG+eq9bur0hEAAVQY1gUbNKysL/AOoevFfh8IZwPxUTzPbsXPfmE1EHqSafAGWxyuPUgzUuk88uC5cvAxcuDgXwBHNZlQB9AITKLto/gvTAOeVWd4QxnkPrr+M4OmRD05BRHSenZzYjoq52x4KlSqxat8wZ9qZCagM7erBYsv8ALl92jemrPYBCW831lY1+OsGMPQje+Fwn2GGWNYVe5WTq4G0X7Np7w4Kh22Mr/FH3IAyHiK8X+rkM3AZcqJMzK9WcpcIbM/dB00g8cIptUMCVwFC179E3TuH3evGg/jy/LUrgoREscqmqpaPRu504jgqBcSECBh3Co6PCx2R/CWS0zneaIEcjMrs7SnvB19j9oROhsREXqzO9iKLHgCOHMA1MjzKplnX3neN6vlX2ecQNmlWbFqLF/A8IkWlHeGLt333/ADVxno5gS3tR6GUvh/1MBlheZpdozfrhuUtvLzl9cbKVafeYR68J8Bmbzdiz4IVnx2CPqouu8LnLR5bQ/BU0gUN39Eslty6dXr+DPSzq5nKAhNaJX4LxvhuA9kL24jEIkEMqBDeDchn7UFAhIaBczfzV7D8PTevtAYZJcSxhJUaDljLs5ZvOZMcwfTgYxxDCidqA8ypoOdcn7Z08gFHd0lr1mfLvhbRcElYJ+K/i/wDVcVcTGe26kTg3KeLVWfpI+tFAyNbygchR778ILOu9o2zVc8LCK8g/hK4LlEN+AGX4GbHtSTV3uZcvhs00B2M/wkJRBqC3wS5WVq6de8v8OtCcy/UJnYHuEHvxmZkd1GN8dy8FwDWie0ZWFQIESC5QIDEYMUDroqAIZVSpXLR+tFb8JLiS1MuTfucUd1TygsjHBMKn2UPSSy74V49/M2GoVkS3EP5nOZIywMuXDgSP4K5bTs4s4GClBP6weJaehHzPaKSe/WR/eB3nMtzfSe9ysU13kffyweCm3K+Or9YZbX6MZy74ctOS8DPDWEKDJkDI4Se3k77RmWauv+Bkl7gfw3GdZEyPW7P47lzMEj0Z75pNSXYfLCM/Acz2Lhn9iHaaUflFO3HImaXLlwcMktUHzr7XiSokHDLMmLcyRYGBHS/eYUEtf8h+PQRp94UXLQlPJywJgmDrerufR2mmw+j3X6mdEGmV2COAWLL4BwDLixxeAh7MO7NJs8W9X1UuXxBcv3Wn2P8AbB4LlwMv4DpWFxmknV1hw1Y5XPdhOc4eXI4q5ch6Gf6/wFG2vdNJoqU4BeN4gDJQ7kYZ3P55y7n4CMcEhx3DBALbcz4wuXLgzQ3n8R98FxzgYDFlQZTBBsGkrm3MgIpWqjBb0u349wlEZCzApizb8S6CJEmXLMGbNaA7/ms60lp/THJHFFj+K8Xhq5kW77Q4qhcp5av4R6ILyZvzK4Gcj2rtAl5TXfnT24uXKg7GWFrGafBB4bYbru/1X+BkTafPbiu9l6iEu+G5lCavdpbOVPEpO2rGVz6QH3Nad2U6XuM3P/HWewGh9S/YNaa/gsAoref9VxWEfpv93whLQhGEsqQ2w6wKbqqgADIVAmn7aco/k5jgdpcFzMUzWNVeMQ6hF2HNYtsZFGbyJ21G5HV5uMRYqJUZZGkSFsCYVH8OUHwDTidfPvxMx9i8/g5ovEM34i4dqwjucvvDSOR8sALIA4R5qV32iPeFIZ3B0up53mURzlZSNc3IM4y28nbb8VQbQnvqrPik+0ECloa54W6aZehKwvC6j0VmZczBvcrJyQuN4XNsFt6EcuAJms1X504HDOm1XsMDhASLoZCaQ+DscOoEfSTWYVeZUCEEgJUOc1cSPPDVCqAlsmRCnMM1bI99/wAr2VX8DDAbHONhJVb+8obmhk1NP2ju/o3PNY7u6A2HIxahSNVw3HlBtNTDKLBAgYL49G9+uB5lj44szq/cvwIgC2ZdEG1/dNOC5a72UaYV0z3loYXKSsrKj1zOxhexfjJLjwZ80vnOX4NM+Hywcz3Ws+cC/c0s+LPb5CDCS3/STUKVw/3EjLl4XFnLBQdjKKX09k5MU/p7rlgsuXjrImV63ngfRTPlg5UBivBrkAHllL1S9X6idN2c1DdiT4Y59TUu7U0z1TPrWhsx2FEuXjczPzgffsQMKhBxRZAqREzMJKzO0WoDDRgqHRXfEDtLX8o1OZcMNkDUeF3082CQOn9ywM/1RjBcUWkGLKgyxC1mrBMoZMpWEJXFVj7jCuG0XzTXARR7T18UnJbok9ma49gbipyP+spQLdAWnniXdkc2W4v05hYetbsS5fBQTkDy4Zw2D5oONwZQ7rdg46l0aLC4FnIND5wUHY9IVkNGWF4ks6j2iObVwS/dPrQEihWdjL4RnEoO5EOd7zOXcxsjVQON4vE8Ny4f3Abv1Bgy8BCy5cvLwCDCvS2zvKyBc+9pPz22rOSnZdkqTewhxeW8WO5mQQRvhAwSUCI5dYBbN6LnBjBwYxlYU+pL9tpVcSPxLIE9bRPynsuJDG443wXuVsx/eeATK2gvc8KMzoZsZTc9MM/7r86Y3jRzRD4/A1KWO0Q4bz7z7uHP4Pl4LlzJrmPjrwUOqDuYLly8chd19+lwZUy5/R1g/wCFoIHhi71xvAhgFpouJUEJJegFrlAoswvugR1oERu5+dDSklhsh5JcpHVn3ShbKTaKOsSGZoqYSgJUc+UJMDDNYlMVZ4LBeDwaxg+EAShphZKxxCy4/iuLP+5tjPX2m7hylC+xmwZcvC5mFWQ7uD5sBB6WHowuXLlHUouagAXllQblU90VnxBV8zVOxJ7ctsPCXWPMAetBtjeDBtjxIwtXX2DhWUm6q88DhFWJszJoZX3eZeNy5c27O3pRg6wXnNH+Df0tNnX+kWu+08IXKwuAsQm9mZDDU7N3zg4QfF32/wAHT9od5UssSXoVhNSLARlqwlixYolIISOIRSyhnjApweD7ZIf6/JcuXw6gNEoIJmNCtPCPga0bXG0TKr3YPBcot3R0MLgMx5bQ4kC8xLammT1z2BQ4XC+ALD5pFmOrhzWBe7wXhnbdD4y4dsPwNyUzFY8VXLnNGU9TzFUIHJIrQX9si4n4cotFuQbwp8vcKbn3vztEpVtd3grAIEEC4+sVoyhmZLIbDV0l1GeAoiRaia09nb/Bd2ksgvdFJ1gQgtOa6wZolJlUGZ0FYGa5o0cArFIrIqlkFAlgDESpqjngSVLmWn2WmN/ku4dZfTJc6DZv9Jq0Ov8AROi1aB2MTC7SlLttw3MvNh89cOs4jB/wRgCtdhHsuZYfnDp7ekOmmLgKui+yJqpPE34OS+tEj2joT3E0Rd/3TQl4s1XwWwQq0L5npM5j6FZQOYtiOcK/mZuBZ71ECml5TS+wj45B9zf3uSHvef8AWfbbYnQ+F+5txD8DS5F7FNa8OU9zReIwjRhgpqIy4N7BR5xBKQLzfON9psf4jIDODeRaDmxeSpuHObJkl1y4AQRlxz4otBTAVlEgxcKjg7N9t4BAoFBjeNcF3B1GOSTnByaLbvAeyezfZ7zVMciiK7SvV49FpDzNA08IQ6DOLqhcBfyC/wDC1KgrQiq4vXv6YE5Be8wQg31w9sRIvRPCFZipbR1/Bk+4J75Ciub64kv/ABAwVhHNEQVXCyQrWEuUfNhBUPK4Fueq6zJCQESB2l9v8Ny0SxlwcpQ64ObfTvLGaykgMrKOccpdxY8oRjhcjF0Yk7EUJpFg4bEB5HXhHAzqcgl9UNoXXeUGfD1b3l3n2yp7RC0XNbwv8VzTWXiHDQ77wcSyRmnLk/hricolMN1ohvQj7pdKRyA4M4IWrdm8jsE16ec975xbqr5/Af5NQJUtgQal0olqJiOtjO5SlmRMnNnuwfSfykOX+KtR5D0YJCVI4lk5ftFLQkrJKYKA5xYuCzgEYsQYoQqwVFMMnyvBeLfGhH+Depodo/7xVA6lfGFYbOs+v9oNcd1ZwXhood1T3iBNffkzQ4D3TaIW5D3xrYRXYeNYBHhr/NriDG5U4CtDKZpa5kJFcyDHIBFKazjUAyqgRas6oOsfi1tf8UbShkipdyCqnazPMhqJgsGXgoYIHATfTqx24tOFmUdq3Yzns4hac6e50lnf/AJkD8s58DNNpVOYuBcavtDlPk7X4n8bPYavPjYCfMgmd8QFYAzJPT1hfmnuGj6ntIfpmrz0T6QD7m33895yJ3hsD3kf69P+YlP60Q3YOyvaW7Pb98+zZ94cGmqHs2H6nucBE7J6esvKrCvxnARjLhgBs4ZY2ai7Y5FHJrEOU3kFDLcq3l9SvIvKUogq6ET9k3/HuZnUekVoTmRzmojN7kyIIkTC+AYbib9CDhKwdR4Z4P7zhxXy1eDQ/wAEILmzYuHVQ6T95ME2m7rNE9udWgvd2cVVubwVgS8KgSux5mRudaM6J52PnDRPkifSZXtiv3DTA96sHwT54aa7JPZBM9gNJpa7BEvdnc+sr1lZSUSiUTKUiEqP5nLOb6yznFOrNXPcQih+iz305ew9fhPdGKfD1+RNGPbfjCMlTQdjX0Z/Pw6RXoMvynqjlD8AgiXCbsOsFm1Dc7Cs6XDinKFLOsfUC/qCQKqEvUfyMZFc3/HJSL8wg0uC63jqGf5OUqgiRPxjBGEjlwt3NYcVjL9BP8K+AwrgCVFKveY+8pM63CfZspKjsJGUrzovxNdPmlfeZGB0yVOhKOBSA54Lc3pPaeye+vD5mHwJv7uE+Is+WfDtDdftp9nUT7y5899n2z58EJcvRbadlPoSj6jwMuKtUFH7ib69gzfSDdaEWsS+nfNQ9E+45MDv+56NSXrU3ZuSnhiXvCeqhHIv6qQuOfRMe2FSoEGcy4AJawAKGyIYtYJMs4jKJalO0RFayqGzms16wgygF2zRu5Hkf5LJU9xHtGmAcZujyecoiLTBBH8zas/gIHEDm/XeK5Zq/wCFXCEqKBYbvXrLITyb9CZdTsPdlGgt0r0JQ2nMK+sEUA5BUpAGKCVd402mkv0U0Kv8Mo5XctPjASP1/wCp7/yPsR33Ngrsz44MQ9xlY/6CVWxM+Bwf8QMB8zsVAgYBBzhF2EUxNxwJhZGhMOsBYARMyGRl+8EgTSbGmdH2/wAutrP6MAEguP2wG5gmJE/LSrN+A478Z0XQ/wB/grhrjCVtvymfHf7qypB8re1lKfkT8AlJa83uGChkNAyJRKDBJOb5h6nHJJYAza3xntPh959w/VLDxL+cRUXIpvZOPVdVzLoB2JaVEwJKlcCY1hUr/GIsvApcGWiNQWDbgAGcQYMu8DNRCfcXSEAKqGU6FYdYs1va/wCWrSjyz6koXDcLGIjvXU5QwRJWNfgcgM2UATq99+JlUNs+Ma/HXAAedmiwAf8AESlB6/8AVlRan/YZXTblMjDsZRX9EmXclLF2dQoJfhnN1NvDkEPLcbPXoSzYt55ocuXaEEEHCdpjuCwkSVEiSuBJWD+apWBguBgjgJHAMuODg3ngMM40m4LyCBc5uq9YED8Bdss46B/zXIzfrFby2cokEBrvygE1l9+scMkCJKgRJUrgrI+i/wB8S1O/rWndmoouBXHWNTvDeW86Tn6f/iSvCcsv2QBy9Y9pki1nU8NLkbDez+6af+1qJWGHXerH/AFHoRz+UzTPAY1I6EIMAdE7YN2guUp2nTj0xlxIwywkSVwJgmNf4KQLl0BCIYC4KYRYZrGRURiDhIoLlDsgmEUCuQS6ynWPt/nq1mejFQbveG51ZXZynsS2NHEXIlMSBHEleNB2P9w4Nc+zOX6l2LO+938VcHS8Tvq6Rtea/wDEJl6u/wD6INHtCo9Il1GpReBG2r2liRsMfuUYJ/6TEsh5WHoTM1FvmxDAQTZLoV2gTkRESOys9/v2p7bR9RdeJMao5H9spPmCZuV3Uf8Af47R7KaV2E+/bTNOshexmNHXpfcsK03zHqRCkeQSN9M4yyy4SpUTCokrFxTgrgCVAgRIYsVwYMWRwQItvLc8CCEIPLDmgDzwdteSPuM1a1/zxiOrGphdWGGLyEhs84kBDAphZYZVzRHHNbag6zSEOKwXhm6z3VEdjaZtYlVrhX4jTF0C1lVfQT41T+SyaIzkwzRbpe/SaQ+X0koWc2/v1nXuifPElQIEIJMDcUthcrk3drDA9GRSu7uq9pU+Zc95WrlAV8TnGUgkM4F7QHlKi4EplPJixpgjQIFReQRfKLdvxl0u8gJdr/WBivdD16zNpnHGEiRIkqJgysalcFSoFxMWFODRZcvjCBBD5pzUPQZASpW2pFbz0HI/8FeKgGuRtBsgAFZIzL5df9S0EEuo2gxmrlRJRLcd9oRQmXLSv+rnxPo7fk6cKuru7TUJtE+TyPeGd4Cs2mg/jkl+C2Y9dZ1c837y+CpUCBAhBK9dAfJBj+ZLaUid5q9EJ7NdzmM5ZQdWr1msqjmz0QkX6TWB6/3PXvGPdomPbivuaEL5D6TX15zWx3/ZABcwFZGVBlo9DkesYVrBUjvP6uGG/VK335lHXeT6muA7Hwz1qnPmau/YfaUYk5r94T1cJjl0ZmsLqNJfKrrv1LkXePRLt/oWkZcIkqVgnFUrCoFxIMWXWK4P4hAgYYO3Ieme7mwVExoRTnPy6/8Ahsu5ohxNZfa3+U705yOczygiSpnGBEiS8j6b+Zlg8nPsTWhb2fc41K/BnYHRg8usy03bz+q+Cci/l9z9TfTNp7JeNYVAlSoEMBdMNrIOfwF7EpWp/saS1YtBqXzpXqxdQBuqlqcsrNydrfsTIOVA+WXWc8740lhoHiDBlwTFLxQhnucmrmeXxLvqSgRzHZJ/aEK+mDCxmuXzE+oz/CUhbTp+/WE0f19mVfJIvALTmFcuBA9pcJX+6NGfwlG5MTinPIyJUqJElfgGouF8RGPGQMJmBaaoh1F6ry6QTRFAq5EuZPsfH/i5tA7x4tY0U6Ru8szlzQmFJmwAwFR1hGEBZu4yiRHS/makIkypX479sK4qlSpUCVM43827yjSf0as7NTSJdi8ywodYVRnSX6s83YvqKHwjP6n4n0Cn0YnTHi9YSJeIYRDAxbiZp+FPuQgKILO5Eiv9IbmAsXEW8EvXTlHToXI9NJW/zHGjKUXLb/pmxPUZ07cbTs7Tvo9l4/adnvsux3jLCRInGsfxFwOAIEDEhOadDsgmYbPL0OcVpa/8b/eAOfeAkEeUI0zJYPbcojQoZU4VVFGDFgqsUeZKiSuCvz1gECVAQ06Hoasp65tZO2iZZ6eMM37KE+DZYh9KYNt8y+wLJS55yEDBY2W2+oekvMz6Xro+IUCrYz7tkRHCMA4Oou03WZfAfJ4ACUZq9vmIAcgmo8pfAFg4PFGZGV05HWGgpNv5IDZ+T/VDhkDKgynx4ZUa1MDpOYh35vg8wUK2fjngJElROOvwHGQMTfIJrb9YIgQjFd5HMYl3Mq/+OjCDSAK1u6de3xDFkE0zSS6G3946MACU4kgQOGuOsAwCVKPnTIPBvKOkbOR2I7Z6NsHHKev6uhLhRNvuitFmrmYcmNNpZ2nUmTsek9OBa9JUnWn0SgS22n6Qzp1HO4EFVmScp8R8/U27zIrBgpxfk3lbKt4PXSNJcr9yXbm84HHLgttDp5tib3OOcSg0w3XIN5aG1vgNCI3X2D6gFqTY0zlAFm/Xt5gUy5iZToH4Z9jtDNjOBIkRBlSRIkSJ+SpUDCokDAIEMEAkAoABKnZsG68pqSNDkf8Ak0pIdKZm6PKUFkGmdZ2PyTNgESOISsRwuJKwqJK4wgS1kOh3WZKhtWf23lJfJpoHiaZuQby3yrZZO7LnwePPOB6S8DDhuEypBjhzLo+OUTUP4xrHBkV/1o8yJXZQ4AcEJRVDMTZg7IU8jrg2iZfNs+sDGmF1IpeI1gOQXgEGCb5GqxQuW/5GuAYM7S6tu53imrryzZnR28xBoDOyW5G0Xs7wj3eewwRInGysawCEJWCS2JTAxMOhNZ59IcQDYgQE8DpUT/y4WmWzmRHXXd+O8GkMdYr54I06ktwBEhhCUiROEMaxrCoECIgCrKgmqOfunlFo0wa9cLEV7uXc7S/OQ5f+0upeBrHGsOOJMT0GFl0X/U6QITnbGXoy5eAy5cGLGa5Xh/6gYfUdP/UXBZcu5z39VJUXTz4YegMBJgZplM/Kt/Tu2+JcspnkvBHniYDxylVdz1/3HWVcT8dYGEsFIqiXi4LJDydCAnBtKgTwXnHwkbJz/wDNBsvOQP36POK4ejOY5n7iXYmqmqaJdys45JUrAFxOIwCBNOtyndZk02hl2NotsVbso3toZ9gmdFdl6jFCOOt4w8MLlwYMYc4v3OOTcYsl0mCzlgpZTmakr6Lu+k6J335Z/Jod5qHclHyyIBue/wC+U/x+8TgVDJfWXg2QsZlIk0DzD6j4Abz979p9f35mp851+M0hEX5ou5Zm/wCyDVBX4ly8ckknMgiiklgDQCZPZt3jnURUsoOftr/gxV1U1K5JEicQRgMHAaiwMBAw9Mof+BDX7AgQeIZrY5YT/wA8TC5sg+8yDtmTzg2QB5xTmcvcRqoY5Yrgy8owiQjwVgErAgI8nh/aHyHA6rvElQAztZl60dh2N5rhwXONcFlwGGLly8Ll4vdCBco7Vuz5wYehBf1PY8owmQq0qAg1FuVLqXwMDgIkDBairvhU1ffdQ95faZuHs1nTcDYLwEkFoQtav169zaBWb1XJNpeEBqjX5TrtEz7B36juRIkTjOUYQtgvjiwsojv9If8ADYYG4AztYyAD/wCiOiz5ht1JkHb95uQT1mUZ1L5IkMETC5cvAi+MIRml7p8/pGpehkBodpkZ3oa9hF19617u8WozbiMXLxuXgJUdhbKxL+7/ANzVI/zzZV5Ocv8AmE9HDUCXNZWIy4i6C+JTmcjmx9lwTR13lrXjOCkv2TQo25mDcqbXatVDUjsIW7RawBKl1HOBF/L0WS4VO7R6aS/G/wA2077cMnzLlwpgXc6jEOjySdwTOnU5k6YHs37OkrfnNHlDvEiY1KhNolxoQSlRC4agQRAAYCAVlRtKgUQM7iIAP/phqK3z+xMubs6/3KDSyAessAH/AGDrHFEpMqeBFSpUqVicATJjEBmsBHUan7IVSra7wJq9L3/qNATd+COEYuLFl8J5N2bZUB5r0Er1dS9gnhqlhXBS6Qu+1BBHO8mhjFJdyYEYKjELRc93VxdY7tzxAOUHAtzWUHx39o82gsYsyTzJ7jOZs9J7NTJpG8mfZc+pQHiFfM2eyLA9qjbC94WwIUqOwslyp9/6fEuKT3PtEzDk6VLhBObILUEIVA026j9R/s5h5B+pzcwNDzInAE0wDG2AwwkUsQj4BtlhzTYR4Av/AKoa2b/7O80XtD9dHpCNmZBNMFDN82lCmSQYDgJKlYVhXA49aBms5rA29L9y5qp0Ht0/aKSlXW2MsrFly5fBW2X+BrKPkal0Mia4esSijLbFsv8ApC33r9BL6r6Hyz0RlqWcprO2U9GpcW55wwuXLlxYQZdVkFmuuc58DwYsvG2o56wb3RV0Yap3DMtmeIx9pnKk6o09GZYfnL+8GwdneazSANXj/wC88KwyPjfxEMIMkFJLhDlswKpGWiB6B/vHWWZ05y9hW02SJixeCoGEqhVrsTr8E27ypjG3AxCjpzf+vcy3q6M5ocs9R5P9nAFkA0yssv8A4Mopvv1MFpgVKlYBKlYNIXQM1mVNq5fkimgDNWXtaGk/oQYsYWXwiHTYtlaX+55Skf7EuDcGEuXR5m+0JY71K9iWBnNj5ayylbq8RxGZS6/lwGdqqMvC5fBnLp8D9Y08ZVX1h4Ll7QyTptjBp5pT5HU6y5rPLe5+hyh6H4D6TUb/AEDBZov25Y/cpBVt5ncwGDoi2Z5SvqB2P7miH/ROkae6tgysElSoECF8FYOSAlaT2OyCUFYEKOkUqQ/9lOUnfknKd+Jbn/r1ghGICnSZi3I17JTjbosnvylmISVKwqBFYCrICBQCPp5HWC2zC6CKUo/6emCxYvD0DbAZf39/0n9AVq74XgufKpHWEc630nh0WvR+JcmWtqfH/uDL9p/Voy/wXRuPzLqXKSM/ZeJCSg2d4HYtyGFG8/TbPFSlbqe+pKtlNlmi6CIjnYy6nekfcEsP2nbswRpmhHMYGRBZOXnNuLmHpdyOBJUCEGF8wa0izVa1dWGaCpdazXIaOsQ9lsP/AG0ZaJJsr/N/pAIN7mGWnJCPeWD9seTEElSpUSaWx0n7QSxmLsilH+BekuLFwvFoQ2Fz5QPlgvldA1BnWg6r/stv5LDVl4EX8G/5V04/WwNFFS5mHqvNZTbjvB9cB6IOnh9RBgc4adzP6l3xV9SnpOzLlrIAeU7qF6DxXFM7yfRNaDz5+zt5mueyWMbiNnEkWxnrRDG0+alhnLKep5lSpUMDTFAuZ37IIcOUqEqoVCVHxxQvNf8AuiVjKcfM07+vWa7yajqQbzhmZFQm4fzUQOW0MspgCEbTZ39URdVeqy7zxnzP1+DNxy/qlQO47vdg1Bhtoea8Sx8yfRtFyJur/Pd5aPLbHJrkvAz/AFL4b4bpeSrwy5lkcxyipZfIYvBqWr6+xlzWZm5eU/CudVeXjlCq598TnFcoXqHR2eTMpsSjMnZ35DlCmkiPxFjms+YYhKll0BnEQh1Gw784VAMsGAgOcX+68o4RV/8AAL6aA/hOkuHnBo/3KG2oAplGtdhrC95UXzyjLhCNGdyCHoDQNibSNN1iXgzt61PklNWjcadiczWDKeXy5rxLj1z9e0UoTdX/AIBKrbHs/wB/EGXKt53uz/Uv8XQMPk/1gucsFPWcefmMvaJVGBOkZ0cX9RuYPGRQGYjmRPg/p9wyxWOYZ4yT7M/ijM5+6ARk0rnyGUlkpHBXaN+h/uXss9dZhFBFqFWpF6crn/8AB23OaujLATz/AIWQWDe5g/EZ7MdCrdenjlEbj1RnLGzUEz8NUjyGj1pUSMTHO3qmn9M0s5aUGddIu/Y3lp/Klcojcd1b/gkDqyAmnE8D1ZhzU4r843jcuXi+2e6QYM7Vvo/gu84J57wzrFufvEtnCh1/DU1iR1Pc5Q/a/Vcme+IUUQz6JzJYWtc/zlNur55vnHQ5a1+4dAGxpAhFrUY8pCZ1fIaH/wAKdcGiQnUvw7v3N/mtL/XKiyGKSbYmjBQegJl3kugcq6peCFqJEiYZYFAij95ftYt94fGctv4W7x05/wCIq3t9+DCLd519j/f5OslvmaS51APWfgYsZgmzB6CPy84Mo8rJ7nj8BAgQvntA6DkzSkNfWEAPWk1EGnZ/uAZebqeIDQmkSDKjjunLFwru/wDxD1jdrdOztNZk3dzufcCoPZmstcpn3B6t+pbGtnZ7RhlKnmANBzZqTWuZ/oSp0lQexL09617u/wDisItZB1gA0L61rgBqtGtxf9IH4rw/oQY3zN5cstf8eP4HzxyTpAAAosTcj5Xzmen2Xp44qgQIEEPgx+4YTd/quAwMtgyhCLUi/I7A3l+J+bX/AOLIC9yWAnN/qpojeee/Z3gC1AyuceIdmXKvV+phQRsT4EK/3A9A9VzYk7KX8vPtFijeEif4lGPn/wCsMDpm9Bufr83W7e2Ed6vpL/DuuzJwdS9z9wlUxaTEgQISSqOTYLjtvldX6nLjI37wzQRQj9Cza4xS9iKFru//ABwnM3seOUO582ki1iwzhOZGQ+vc7SrJ0R+6WdZzAImUNO49uWAIkT/DLm0oJor2vNuwZUNArTsTtL8jhX+AlovT2HDzlW9pp+AlJZrE2YYXQYNJk9lZD1PGIQIRfRquxKwb+erxPIxnKA5MEow6sWdh/GU8Cg//ACKsUjNEh/285cEcv9m0EEM+sM4RmQprXLlFkvWZf7RGY7CowwkSJKxr8t8Gx0c2Amgb2jYefz2k5bD5VqdfU9/xeGH7xgrNH73TzIFwlEAq2CVZt/ipRWu8ZpZ5weIZRYOoaeOcHmPW8co4Ur1/+UGDe4yz2MWu/wCkTEW0S/8ASGiGGskqPVNzzLWs8nrOzvmIJEiSvyo5lmHKK5UAMgNiEaXBfd2Ipdrb+ftQvqRmhuSTugxrgrAgRbf5X+kG55mnJmzEqLnZiWf+kr613I0sz5wyh9qc4wWod2KK+RR8b+Z30F/8x9FYw0FudmfjaJh1C/8ASCiGfWGc1EjTluLlmPO5xY3j1yOIJE464EQFrKoO+Mzry8S5btvN/hn8n+BV32Ms6jmOFWFQIQQ8ls2NSr7QlB4anrPf7j1mkqwLzdJbYpq3Q7sza/x8stA5DY8f/OXfvTeUFn+h/pACTvnqakDF18mEEHXDLRE7dlj2YWMeUGUTASVKicFSpU0x2fcIuZ80fg5/4NQcx8xlYc7zX2lQlSpUCGB2+olA8jQpLe++0CMijYg6SpYaxAMc2M1dV9DdmQ29P0/uXB66Gx/9AcG9FVNUDm/6GVnVr4dHxE7+CM0Nms1AhygR2ZYBvvk9pevIzmjdeNy9ZmjLKSpUqVHPQM3yJZkBQ0DYl1FnrP8A9T9Xx0lfmrCoc/tLvDO0qgYCKqMWzoSheVHNKXqZyhHk4Km2So11mSEcmr6QLfyHRp5mVKvcfTQl2T3V/wD0lsGH0/f+yVBr/wDZ6kKpzNX/AIT2uJorhqAT9Gy37xkZm3p5uWtBOeLVAKHEIi1lU13OvORYOgmZX8FypUqVKxqVhUrAJUqVPfIMUNj+ZyiIUn0LmkVc8sqvSErU6xXB6DkBNrgVHWoRmMc1jJ758jOceM9X1Koxdtb51in/AOpLCe6qCRzD/pJUD5B7Jbuj+qmtLtN2muko6zZpkKD3ly2HfJPZVrPZoVFtiChqKqjJpxPLLNiIYKlYKlSpUrjAqpZK7FYzWc6W7I0AYugTkRovUZ4YAXITYJogw1MECsh1ajdIcmXqYttHoLerlH7qeZf9e06zyX/68aZbqmaP6wr1EJ/XvWVFrO/6s4z2Qah+Q9mGkZY4WaibNNz9Z7kYmsH1yTadnGxfcGBcrzBNR3snMPlE9ztDyfon8jLv0ILy+MF2e4i9mfuCZ9ps397BD6u+z7mLKz0BANjtCYOACWEdcINaPWaguTMm0n9mWs9Nap6ucQLH8VzvvJf/ANrbDCm7/gZvR5pf6j/Eh9SfVl3oz3aVZNDt65QbIezN2g28o74IciLbMJdHRjyZ0Z0ZRthuggGyV7PSANiZSkq3I7ia+DzPshNe9tKch/ToT44p92b46ng+y7K9op/+8ue7Ihv+IM1UebA0x/U/aPV3/BvGVqeQzlzvGwDsn7hmgRDSGkXzLf2R/wB5H/bR/a4/uU13159XzSG7EJpf2m0/M5Su0XPrDKaor5Itl4JrS93/APCLi9oPTCA1e4udN6J1fadk7QGOv9ccFeU6+K75bqv/AOhP/9oADAMBAAIAAwAAABAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAADPF51d/vmGZGICzrwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA3gWlct0YyWGaHnst2NzKdFbcAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAD15fruDWBHLIIIIIMIJIJ0v4UPpZ2sAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAD62x3mWkcoYMMMMMMMMMMMMMMGNNH+DDdn4AAAAAAAAAAAAAAAAAAAAAAAAAAAAAgAAAAAAAAAAAAAXruuy5Kc+fXcIMMMMMMMMMMMEEEEEEEXDhxyTYAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACstIGVDWI4skLtIIMMMEEEMEEFEEEEEd36wUHl3LkAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAD9J7kFjub8r84I6aBoYEEGEEEEEEEEEFXDOMHIFPD1ErQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABFe4oA6URyLf4KEBxLT4kEEEEEAAAAARdUPNPMoE4EOAKLEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAm+gxGER4233snJiZY5RwmREwHAAAAAAAEdBuwtdsglEH/FlzcAAAAAAAAAAAAAAAAAAAAAAAAAAAAD1YKDBAUcL2Pn/AFktIzGbUjTAWAIAAAAAAphA7ArPBBQBBS6auAQAAAAAAAAAAAAAAAAAAAAAAAAAAA+aN1EgAEp4gKw/uN0D9xiqk6/CAAgAAAEBkMEcajHDYFAAeaNcWQQ/AAAAAAAAAAAAAAAAAAAAAAAA4JcDgAsYmEv3NLw25g53YzUCBwAAIAAAAELQEH0lPTflXAIJ9IkwAoY9AAAAAAAAAAAAAAAAAAAAAA5gc+higAisW1CDg2in55ZD5uYBAAAgBAAcRyagEIwcDD+SAEyftCAH9tYlAAAAAAAAAAAAAAAAAAAA/hTiTCmggsZ2oCpvvpLM7Nszp4AAAGbRAAO6TykD3QK/rXPgEkYLMlwXwWAAAAAAAAAAAAAAAAAAAA7XjN3GS6CNdhHPCzwERACSMmgAAAAAojxAAgicoMXqcOeDcPeA5zhSv1gpK7uAAAAAAAAAAAAAAAAAAbunA6F9Z+gy6RBGFfIFSDGBBAAAAAAV7CAAAavfqwABH34eAlVG5REQppL361VAAAAAAAAAAAAAAAA2I5+d4odN7QWhODUZy4MCHPBEEAAgqF6i5AAA01JCPcBWsfZPMb5q4LhGexCHFJ9AAAAAAAAAAAAAAilJMdAIYWl4FWZrrD1hib7jRHAyAAERQdWIKGGTru7fGCxsWWeYVYu3nhIrWeQA4LAAAAAAAAAAAAA7RaRjqkVX0SMJBRX7ASgjykUuHkO4MCCYBOzKcJj75QYikIivbM41qLBtB/qCHJN5l+AAAAAAAAAAAA6EEXWtS02PK7AJYNKpCRl8SiACx2ZygADAWbKKBpDocBIfssqEKhbdyZW3UeWfNfmUm8AAAAAAAAAArTFRyuc/RNB3DpSx0jjAQV75ALSDmqXAAECZFiUP7asDEZUauahlOEJvRcaRqQr6n3CthAAAAAAAAAAzF4ZQx2EQ0HVXEmIA6DIASpMEBCCTHqYAAh3w9LHCMII3jhlehCjfGQcre+g0acqQJzOaWAAAAAAAAi7/ANGpq7l58SgUGEChINpAACoNZIgEh/IQABIcURxwwrCY2OgQSmijn3AgqiYlgyQKZjTGrwAAAAAAAGXBYiis+MpF/SAZ5U609yBgAGo0SQgUogCLBPqT5jpEALuPYCYWUL2Kgo7T0Sndp0whkhR31AAAAAALG+SYDPk+FptKw8Yr0eIAnNggBgTl5GTuArY4dvwkSlcJTQfIvzRECX08poGlMYyUAgggkTBkQAAAAAIsV4HPPsusAH7czLsMbhbCNQggDbg50ef4acIEvPmGtEDg5kGthNlI4q71nIIqPswgwggyldhgAAAAAFh1AYSLMvjTVLT/ADrOwRqVTQIYQsIcGPAvHVlSBtOABTazKhIFvXz55anTQGM4CIRsdktNHsbfwAAAC01bIFEDghLf6sdjIwyAZUHAI15YoIIkKC46KcmC3mgBQMACh7x0Qw6z4iSyn9WrTEGTZI56ga0YAAAAWdmEFEBABjHLCxQFI8At1A8DxQIMMMN2NlmOeDwiAAAJIAAAZJAbxzAyHKmrSsMMFZAWOATNfGAAAADO48MEEEARDgBABHosMEW8oJ58IOMIMcpF1gkUeEgAAAAABuZfoFETL57WeBIUMNnQ3ANiD9QyyLAACgpYJEEUEAABBhCgUWIAfpSr5RtEIYIJMvP8sKaRkkUAAAEEHwCEGONAJNmNMMMMUzXN8zcoQCwgBUAClmKA2kUEAQAAAEGGIIAL7zwJ4qgMsAKNNe0ADJzwCsUEEEEFWMMMMMMNVIIMMNsSQAEfm7RhAhhgsADB6wRqUEW3BAQAkFAUGZohBSJ5ogoIIJPR4/xYMBIIaUEEkEMMMMMIIoIYIIIINCJXyepDUOUDROQogCd1RXRdRwQSDSz0oYSjEmpxD58nMwoIILI17Tl4yIIoLsEc8sKIIIIAgg5pqqam6Gxz9RFi4oQn/SMIDGeinsDWYzQOSwMUdAZaFrcWehUkGEkEaYP9Lugv4UCABxooAUwAAgiCjAAAAAAeyys0kCDHspsjTLYDhxpHzsgKMdDFBnE+XmCeRfMYCOEkAAcyjIIJQSxBqjZiDSCDDCwAgBgAgCggABN3Aw9w6dmC4xuiD0BkoCSowDwfUTmTNZ7YfMVljBAAQAgAhxhwAAApCBqyCARgwQwABLwABpSBTSgACijBFJJbQNvoACoN8AWu5FkAsNcnrSl0Se33+CgeY6iiDxwCgygAABCADDoABjiDBwBRCABSAAAASAACCclABoCu+aQAh3p8Ce5vnwUAkABIY4AANN6CAAAAAAAgiSgACgABQgADRgAAAAxAgAQAABiAAABDCwQoMFABFQgoAAAzMN4AlE1jh1F8wQzIOAgIICqABgDgghgBWABQAAACADgCgBQ6jTAYu4RZiwAAABT+NgMMkAARgABwElJF+8CHJiHLHTvCRgX8Y91OAIADawgQCACIABIAABoApJCPcYxCgwBSTGtMQQwwh1bIoMMEgB0hzDUzyv6psAj8oMMFTkwo6eZb1SIAACCBDDCCgT48NgAAQShTChRhVBygSAhQOYCoUBBAgjsoNMFH1FSjf3RQvOxwD6mgckEkFEPrbp/gYBAMBgDzDChCBbfQAAQAPDiDxBuCAQz+MgTcAQAAQgYDSAIMNEVnygajwiKbTX8DwzHGmwVRX+jreoIABgduTAABASQOLg0PsRszAjEpDoABQuLnRBwgtQlRSDAeoQFnMDvmwpWoWmEAQICIhPjWVIYFE/xlEIBRdIcgAxWPwgxRCRAABBRQxg1YAAAADigAYiADZQaSUKKrOEEFUF86JMkENFnuAD3MdimpH05+v0EJYoqMhBCAAQDghQsEMPweu3ACBAIMoAEkQbCSgBuNFC4MEEEMEAEV4OEEEEE7tkwAC0w5r0mDVONMIMpoUcvNBxS9yVQADDCAy8SSAAmDsJmIAINLKlQJgEQmIGIDeGJGnM4ksEWmS+eVrEAADcLhb+iAMMJw2dwaA5TwhBiIMwYIIYDoLNqXwGFEEEEBARUsE2qMAeMCAACwIsACsMMN1HVS7HLD0AAAJLDSHUMNsYtCAdOAAA02ZnEpIEFAUbLEHIEGGfEHEUFD64AEoo0kEAB0gAACNOa0NMMmNuqaRtmAAACDOKvJI8cxRsU08MIJANEEEEC8MEFEMMELpEEGsAACVrISwAEEmIAAAASggpEECI5IgIIrpIJG1IAAAAARI/8AeBY/zMII1DKCHjBDeRwSMJR3BAB+AJBEhAAAAAADOIMd0kBAAAAn7jAsNAAcLLLHa+E2SdAAAAAJZCpyu3DTDLPjrKCRBBFBAAANQBXHABDtFjVAAAALEDKjDe3sB1KAcT70IACMhBcOTRkYUDHpgAAAAAuAHS2G9iBlaHPCCCVBBJJOJjk1BIBEQpBBBCAAAioypjYp3jqGynAABV7ZzCBRjNlEkusng/OAAAAAAA+v3yoi0OSifvOOZHGMMRHDMA8yEhAzJXgBAVhaDYbl6BHAAfDCWpdgAAAAAABADmGCC+cIg8AAAAAAA/B28v3ITRTsKiK1JAEHDCBDO4APzxUQFDDhDCBjDKipDQLAQCPGwq6ELNORdaDSKp8VOcFIAAAAAAAAAMU0rBm+nHPGqmlwEBCh4jwAEAxCDIg6AJBADDDXBFPOIAAAYxjCAAAAAAACWiBuO7QV+V3AAAAAAAAA9Lktf4KjyqWe1jBAJiAAAAAAC5AEASjmBYgxy4EoMEMEIIEAIIIAAAAAAiSi2hUSvR53/AAAAAAAAAAAxCRrJASjS+eFACDJAAAAAAATSCxEAcMk5JZ4YYggIB9RNd0sEgo0QoKqCauB8YbJgEwpAAAAAAAAAAA+yrxkAACCCyTDDFjAEAAAAAIE4gBk48ENDuJjdNcOiCABBAQAAAAGOyCi2uPkKSXU9kAAAAAAAAAAAAAtDNCUoOCCDpDFXAAAAgAAQpN8eT3tTrxVMbu1v51e8iuABQAAACCSGchyAVNqZYw2AAAAAAAAAAAAAAAAfslgxKOMjiKCCCAAwsZugw4sC+N5FYHR0wi+3owZ4IQgBoQACCjDnBCBxHC0ideAAAAAAAAAAAAAAAu9qYuyAWOCrCCCAwYQxKAFIMe4O8DWkc9wcrwzBTWenNqCEAgC6iieKAAMEayk8AAAAAAAAAAAAAAAAA4yvdtLXgGO2iCAQAASKAbZ0V8+ogWVYgbXOaCeT/O0/o4CBwAASimeIg12Yq2AAAAAAAAAAAAAAAAAAA/XVomDAIm6iioQRTYg45NqlJ5Iu+3SiBGdNIm7VgwBQf8ADgEAQBiqy6GYExgAAAAAAAAAAAAAAAAAAAAPNqsSPPiEF4ACAm5mAO1rCXDpS7WMFCi3RQ6rEzw0OcCUCgDGBEGOBwH/AAAAAAAAAAAAAAAAAAAAAAAABOy9znkNRAAl57k6oMrwBAEDlk/AqntGYjX+pFCpkUiocxxF0T9JZECMAAAAAAAAAAAAAAAAAAAAAAAAAgszhCIcBAX9Ad9KEAUjBYgZpgKeMGnGUwZ0soeOG7z8LCgkSG8x9UAAAAAAAAAAAAAAAAAAAAAAAAAADyk2msCQjp0Y+IA7zkcKnJILNWNIMJL8EIJw6KMSyZFmLdSk+UrQAAAAAAAAAAAAAAAAAAAAAAAAAAAAB2CHyQeKCw+gUUn0BCUEEAKSkAMs8CykKJKJFGAHCkC3pG2oP4AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA2CpLjfAUiQARMAACACEET1lEMIJ5m4EEFJQkEAAB5NcS3r0AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAD3psH6jLhKIAAAAAAAAAtGkAEAEG2wEAAKUEgAlPBChZn4AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABwa+NzmsUAAAAAAAARd8AAACADOoMAABS8WqffhUi4AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAClAwmUp8IkgEAAAAkiAAAAAAAUSMgAEjwA2VYQw0AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAwjsvzJ3m7w4JHzIAAAAAAF2XIA6zHG6xbhwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAADz/ksUFJ+pMqkAg1gZEgbf0ajAezjeDcAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAByZTQnft9z64ejCOrPIF3F9wB+AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAD69V8/DhaqCpCRKwDi64AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABCFI7HLM9OAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAD/8QAKxEAAgECAwcFAQEBAQAAAAAAAAERITEQQVEgYXGBkaHRMLHB4fBA8VBg/9oACAEDAQE/EP72sJJe/pHuZP4n4PrEX5dEZ4fM1TzZkDubkNZO5oXmy0Q5l01ykTvuMotvKaZepcU/+4k3RFaZGrou428e5V7mom9/kU1PAhmlLQfLifYtzvgvJcJ0QvzP6Ptof+95F/teTN94yJ1Y9fXMtBORRoeNCsJfBj1ErFyo+xVXvvqvhlWRLudejJdm3/8AWYVY1dF1IORu8mWMW+76soSQiVUj0VfoUqc9v4R8MKPsqT3xZJJOE7MkklbaC9fOpHJfYRKm++wng8DIsE3okHJ+q6fZKI31Xa4xoa/6DGhCG59foVZb0/FiXG5FAc2i82JlBNbsby18WP8AjkcSEyMZHf5Imp326ikjNaoVU51VH+4ku6Omf2Ny4a/5iTdEQPO7vghXd1Or+iEZLeS8pq6LyzTzRUQnZJkN0GohI/kklxRC88vBCuhWx35rmS7N1muWfIY0JX/kuJWNTt9lKN6/hZFGBqyfVL1fwibRsIiahFghnQi6wCNlfHwLKxq09ri0uoiGv4mkwUikSUnmUSeT5EGQnoLJSNC/PUdSk6lb6/4y3KbyIz2xzIQKiXRElxrJeSqd7LghJHGCrztx0/WFxdKlLLlqxfaYK++vYS5mWST9CbltwruLyIop8avuPZZlk+Q1ykhLohGvXQNHFN2QqEiZReIT49i3AlPoyepvo5eB0VDWX/DkVDdv41In1zebJg1cln9F9blfOpImESKUN9lr4/wriS3nmx4V7DmRJScZrLjPWhWem5PwJDt0FDh4piCVcDJ8xdvX0LmVb1xq2YCQgjEVuG5rgVgN2aIARkef2iDpOR5P/gJNuESxRWWZ8dwigEk5JErU6C4eRunN5kiROLTqxDjlH13l5b32QpqRpQuopzRJmtOYyhDhGEJUVPiir72o+hVDcWxfIqbJk3HC6wmsGP0YEsuBYLG/aiJAaFiZ7D4DPMbJkl8wuO7f/ciolvIiFewvLHOAg48q+PjEQhI0JWXfY+MIQRNdrFeXZ5InpKoVc6rfBJAqF2IZRX6iIeBjNP0ckWCEhIwTaE52GNgrRkBOnJmXHcUdA0+jJZVx8PBb+tVVLZRtXu/hEq65LN/ROumSyWwIKPzpkh4TystMIATbwRvhRPuPg3pa7mWT+zriWhLCYFiDYFmfoZS2M9klBYpmLFw1MURWMtN6FuQxMIjOteG8abNFf6UdUt9t7L9Wu9fo1y9l8sany2PEhCQ5CfZC7jcuR2fRaDcqGiEEOrmJay5rVeSBXRqTPIw2yR7bcbYhdgUiMbBycEyDOr3QkPluwlyIzL8v7jGaKr+dfRLfbey9du71fguGay03vwOrpbzG9khMN+GFwu7xg8EZt2S8lxQuua0EcFB2ZA0uRJYeIeqaalEibcb/AAS4DfowB0tsSJJpI6oa6QLWMlQnFWLRIngmUpVroQ3y3bcLRrouvy/uNNOH/Ktrlsupa71+iE1NZab38D8+W7k7YelWgXMjbixzdXhd6ewh471WadmK2ZtHVdb9hJ2R8wbiscOjqIqfY7jmqNCsMNjY3stoTnGZEkjwgRZkDB4SieopC2FYsDCE8KDr17oXGS1iA01yfK+f5EBctkhquPTcvku+az5e4fmS2N4TihCjLRw6tC6XJDveFW4+OFhsknCSSSdmBIpOUkStsYEEsxFYLgXFG4KEWgkN4FsN0WFUGEyRiIdPTe3bxJSSmLeHWWj8afxxq67LRa8WMNpW3vQdm1fbcN4TsIYeo/JW+CT8j2Bblc64V5dz6UFEHg/SdAKMyMLYRC5mYJJEJDhUEdiKmxrWKdR3uDXxLMjPgm/eCDO5Lyf5iG/I5LyMKPz8Gyb3CPdITS+Oq6oWDYRESqSmT0+hLTXKdGtUZxNZ7vK/hVC6S1fhZihshKrHKxbIqwkeKYhsDLqpzo9k1zwWUG7U7/5sMaHi9mfEEqSkuhPgVGwkNxJQxelzgWMsxJGyEuXXl+Bbls3UIU4LRChFRvP6Paw8mRHJ4E9boeCwE8COgMaSqmWtzzM8Ua50H+q2Sy4jEIxare29HJ5ej/XFsw169vjN6IT0wlEivtHU9OC2ASSSSTgnhJaeNOKqvkugpj1JdG/aKq7RHR5oa8cW5rVbiMTAgmSGHsJeglLgaoikoSSYwV2Vw2wbicnQyNY72QXTayQmcjMkPqpFgUBYE5eFEMjGUg6h9QsLmNnYeTVC7ezWjKEa+9fXt6zGVxkHqXwRQt33LN+BuasclMKUkTJJJEExhSptEiyhLXj9rGv1c+j04MlQ6PBaQklcadDKnGnsTrX3PvI1NshyoHq18q6wWsHhboyZoQEjJTmOkFNisN977Z9hmiCeHsM2m7C7mFhbjXBSlgYOLalitLC4s5II1bwVqpP2E7z1u1XL1UQ+hvf0PrzGZ/VloskNBo+FbYVPELBYIxO/pu0ZMejBBpNMy2NeKo/gdRPFRVd4aLR6jeJcd5mr5pqMBrGYHVNiwqItkCwn7pNObsOnMednx0HB1YuLAgZkhoHUWGHChJkiQ4WXq/A9oHoNKEoXBsCAKpehw0M1Oz0f6g0phqj9NFFWLWafnzJE1FXi05DRhaw0WeBJhz1JVgILAmNU3hiVxGopi+jC9n2FIVDlp5pVo98FFKXODUYzghamnC7m4mxQWQLk4sMuYtlCcVRdSrNq8a74HVKKsq60aehH0WiYa9iNZ6PwSO6sjj4ksEKzi/cd4783knU1lZeS5b5ilpd/ceiYB4DBSpqHCxIXAwPUcjcOyvseaJml3pxZPn6cyutC4Zv4HNvlvY0ulsu8Cw3EuGCpggwZhhMQxkwdGSKs9YqjoaOYKWU3jTkTRnMs+Ldv2oZnMgcbF5HcJHoIaUk6ZiDuyusik5rKUnREGkms0oJVBy8xDoEX8QWQd89h4UIkiwnIGiUEJ2XVcfssm0eHyZfgb0c499sxBVEhFSfLmOQNSPASCIY8sbVkLerGGELCmhxaMZU1XfY6m1k1o9xF8Gea8bh7DkIVcQ9jlnorCKg+QzIhcaSpm4uKlySMM9gEhmPYRhUNMWRdXjmRrrb4P49GHFaFwzE5xZcWPZtvYYs7ALaSR8geBxsCWCFl32MPNJ+6kWTWw24p9nr8C6YqI1VJJTiK+dMG2jWI6R2xg2EbF5OFYUZGTCCgNQLMMq2CSDAZrJavYaCoOpcfstqJHPJ9S42nG3cRaCwmkEeynv8AobF4INYDDwqEImFCYW4gODz5OpaE3hb2/pbCTQleyT5iQl1Rp23nAyb8zyQqs6t2EwvwT2PZmnamCBgeggyiBxENFR0EiDrt4wxNccZr2UOZqqlkxJ5kK6lL4rytuF1KVxf0ZKinwNN5jIIxaGsRGAsBBBCCYEV8G7fQk+dhVnrN8MvJovZcFbBGbX3WjOEI1q04aDQmJBGEYtXfCRBC5wmUY4gbCnCIwLYMPMmpdkG3ETfepcMzWhVXFeVt6kuri/oivOr4ZDbUDEEECCQhISFguKOAcDfLGnKLd+N4v0EuWe7zsM3Smj38RJsSQ1i3hJViWzYgmjDQKjUFgeqwPYFsRQuQegxO1nR8GIziuzquD/Rs6CTXgrjJsh61lwXooIwgSwQSFhoITar2HXrTJK624pgN2Wr56IUY1u5PJIzNhCuQdOBMwtcvBYPCBa1wbrEYpKeyBkxKUxvSCwVCcZJJJJEIpGosQ+KINVVV7r52ar8lC53NfUoXF0HnYexBBGECEhLBCYXBR0mvDMVoKzeufUZxMyFf36Ri1o1er13ZDszeXnt0zqI12p7FFXQpVt5aPNeMRF9lqabKkTJYOWzOwjZZJI3gTwVCcMqr5OBcriq7MKzl1W7EU5uelF6qBLBCEEEGIjUVyfvn3Mqluebglkhycv0Ec+jGSDNsmGGVDCNt1iFulUEsZwJRhHoBjY2PAIUiCshe806lCN3jfTLFybzcCS0ES6GjiJee4/4ExLOdhlIe7wfoFNlsl86vftHihJnBpAQuB8KcYKMg9KQgbKCr0jGxh4RGKZGFbAYqMhK5AmQLkXVU8YwvRz0qPMHss22Mn01ikJgTjhM1NaxlzHLufCyWmiMkHokhLD31aX7sNv1eDzZHskWNXMya1wfcsbnCRLuyGbK/JVFklMm2WrJ8FTYmZJvG4P8AmeDGIhtmRjEvQcnKFQ4CE40Eby0df8xk0Puf+nFd3pgZHorGBLBYxZwxSl9GPJD3ipc2Vkrk8lyOr4Y9XaP8mKrRmq/ZEbCTqyUbcvs1hylxdTSpZ5z8iZaTmRDiGMVHiqHd/KxjxUppkHt3FaqiiNkAsVuraZuGU9K+2O/NpdF9kE1aXz8Y28TE8HgsFglgzZCuK9DLi+EP0aOiR99WedGaTbk/Ze9fj9JxwJXXFCBTIWjqTBN5m4kMa+/DENrhlz3Is/kgt62954TGJke2+qMnkRVJ+05jgiGtlP8AFBQMYyTB3xBYRCGONDtUQwzTXUmFhONTfx8C1qW+3+jkGBMkYsE4LBYi2ApNfvZ5fYby578uo6gN6Q934MkOshpcAuw6fKKmE3up8onVpaWf2NdoiMGL72cBItheXOhkWYW5Zscq7C8+GilEmTPiIN10N16CPWeEEDQgwhAhAhzianVDUDde3vhwMnq2zklvv9YXGoLokqQOGFhkCwJEo4JXKsoSdX8Z9Op1pnjPnIgqVavwXx1wp7FWT+/uZVb1VH4Y8l5XfB7LwZernqNRiqYTbyZbhizstPQl/M8GsYGEiCCMXDZ7lLHNM9VOEQ/EFL0+TwMWFGw2BngplcBMQZYIbj3CKg+6z/foEV4s3X/CbVL1y+yt03g/+Ux4PFiWEEY8Zl7l8iZql7LCB6ewWOAY8WiOmBoQSIVIQtuVe5AGz0SvFw2VWu8Rdpss3xgj2aWjGLoNE4R/yXtRtvDN+B7aw7B7HtoYxk4LFC5GbLVnPBZYpSg0D6QnxPIAJ/X/AMmcFtRtoWkvXAtcF84SP8QRI9UwSMiRoSxWDjYnz/Av5Jxn0JJFb04IESI34D9qweTuexGzX5MaIGLZrDqOqOV+2CQlW8lf6FWDerT4inuNLdI/oknaWLJJ2YYEk7bQiMIwTEb9GvcaXJlaJeywm259mzvq9vI9sQmFlhCjV03TnlyJCqvp9LuMD5b1pJ9SNidlJJInazNtsQ9hCOCj3L5Iv1FGHCDa+fkl0/uX0PagjAkFw5seFVdn7N+w5tq+27YkkkkkqQRiydo2TsEixZJPpGCRIUPQggeCSrFh4Gl9iptm+dvfDds0+q+jgNp/AmLwQ54arYQk3CfYjFFauf0QFXl+dh4SJbckkkBDY0kiCGrtyQm2IECBGyk2XsfI+ERpbmi5EuZ9fKWfw+yDJTJ5YHhAsBYHAnuENck30HkeH5Ehm8diAeMpnUE+3525jaJppd/Hyc56cLsQESuYeu4u42pRVC7Tslu1YwqjIlZbDJ2IECOOz0dTZ0o9Ri7CzyLAOvg+p/ZJY2sqKRMRLeQkTpeisLYQuQlWRGysLLuOGCNlTeSjocEo60+cZXq460FiJu9bxJSZ7RTYbLN/tLikInr9EyjbKIylmU6vgJpvU949vl7Bvoyx0SRLQmcWGIobVmUl03Jmel0EMma3Qx7wkPBoblmt+bLQ9BgKkk4UbqYWtLd/CFRMJYN75ay1HB8tiUv4lpsNV2WfXMgab0Tx4Qd3TZWwa1OvU3kOy/1YsQ5NMTaDSfUoJWp/AmDXQ3bqav8AWLbUstNlIcpK4uWablowu5NjGEEsMSC24Zex8ielLeowR2G2LlwC7Lgb5mpubPikZKEYwXi5vRDpmJX5JLDcmmNUYwVL3COqEsLnGsv2Q3vlsnnN9syBga+9ZcSYl+sF43zY2xMRZKe5ZHqav5MS5b12VgjNIaREWESN2ru/qNiZTWXRbsQVduzx+DdKVfjmIVqxst+9uGPL2IrM9FLF0uek/eLdSiq3nV/uZWt2RC2bMpLh9GaG5ovBLmQKLfMaRFvywd3Igs0uSErIhVzqJ6pbFZJLXe1OKkHnC7mmic9K4y+dkvkZFEZVr9CxJhLDWz2Q/PlvCRVZ74sJs7GtOaLg9C7y5Mqtp4QJCEiWr8DSM05bpwVF22Kz86rlc3+qnEhpAjIv2pOWc35n7DHMrsKExsnIm3eXBvGu2S2TtL9HeI8a+y4DNXc2fEoy02EiUpTRPuTN8jsvzxg2VAmC3L5xZ07jyWiXfwWFpcF5Li/t7DUbVFVzjUTxfx8kzuVOuNUbI1e8SsWEaVXsvl7hgfLeOeFv2W07xFgnyEMNVwp7E++Uus1jGzITvdoXBGrqVOLoh12NPZh8HRibCSBR1XMQxlX+CKsZJG9xELaGY6QmeZlxcTtzsr0XzsyawnQJPHFL2IVzb9zdqu+eCG2khucHJrxE9a48WY6U2EmS1nMDttdWZ0lwRLKWaiVbMsvaHp8t7EtV7TF6aHzE2zwgggggU0WQXvaQhkzFJMnfMgN3q/ZfO1qqqPivKhnIY+AlRCJEl+is2Tamtmyts9+EEEbKQDcku+eMEji8lhRcvMl2f29i8b5kWZV6EYTvN06mgE14KoqYSvN06m/R9sygYbjV3GlztvXmMi/JJEiMMD5eyejHtcNbMwXU0fPo1pyRZh/uI1UJvNPQgg4Ox1ptLCj7VfgdsZBVlRcFtSepUuK+vYTaSQNNxCYuW8ih91snEOzJfsN+lVCpU+X3ipzG+57HtHy8bTx+F8khKyjrjSS9/wC7kyWTu8EirFW7L8tmGUljFBlJ6qCAaV6fZBBTCmq/dB6NGQQUq+ZDpg5NhJSXI3P6Nc82KwJnPGRTqUQvnGBHdz0/3CSRVIxmsVX25EJurouf1t3lWktAImISPsRSozDK+WxjeDQ9uUVX2L7xoV1oXMTMvI+Rpzi0h+okuok213IHqJPUVFyObDDEp21HT7Kwu88rEEFIui977CasnCks25IsySI2yT1/wnHKi+SMEaT1fx5K4/E9SCSiCCj1V9z8PDesjp/pBA6ZZjeav5vyWnoSW1XJCpEqEEC5WVR5wjBRiu1+/DMdJ5CoiGdS/i/QkRWpcM/28uZ5Dtu7sLqPARrDcbLk3mxOREQSIiw1LMLghIrEEEEDhULxkvJdYbS6ibISggRaCTYx3NvcSkySWB5A/VGy8jLsOTG6VPGBGne5AyJtYXKhbNav3LCDTSC/c8Kj0W3r6IIH5UpjLmWe76Ihum/RZmS93p9jc12JBCSCUZVoXAyiF3yHuu36GUM68MxCJsp1QuAp7iUSEggHIMJBFMjWMCypX4IIIxjBdLwWIfCvsLmjeZ/Q1w0Uq8EEEezo/chLPWelSMW1S4IsCCCMXBmUvItt2jqKshJLBD3yN/5vBcZDVhTaiUa3lwZYFcyWTZHKox2isqqaPgIjraOgml9RF2X3NW8mJ2bfISszkvIuqr4wvIpbovS/UcqvYG6EgWWamvYpLKoiNdRVfHLp8+lOPzVw+hTW5cSuTuiBlxGLGMWE4TLqr9lhAhZaEWB9/YuR2EpEuNfBpzhQeyx8XsTDm46EYTIO1X8FJlTHWgpbUlUaEJGzh25saWS2awp4Erzoj/W+hVVSajrsXCUl2YlqT6bUYOhTCSfAcjDgZbdt7yGhst+ll591oVu6iKyXR344B7DqCkNEF1tuC0qkEYSh3v4+fQRwXl8XUY1XNHE46YJq4rPP7FEttci+Dl9mdObG7S6vwWJpcj66vBRmPn/RDZA9jHgnVEKTsqyRwrFS9Xe8+lvUllt68Wa5jnaY85WA9icJKdUWeeRGDK20ThcvQgroF0RTh5Loy4U9i6j5van0YIIII9RYVCUkJpiUJuM0JcgGuv4EPbZS+XyG23L9RbbDVS4xZNH9n+1y0IPQK3Va+WWO5hU45DSn0Y206yG8hk4yJA4DgwsQQR6yZOCjgJt0wCVi/MxSsQlVse21ktF6zp9E188hNJp0p2FRXwP7wWHsXlGgtFJRhME0BzWN23GE4phrwVaG7HaD2LcFLIiyG6DlkNWWIMMPAj0mJHhBfEFDIux1RMJWRJWou7Tl7/wVJ4vjwOKaMebGT3YRBrGJFF7vCfVNFcln7tXfF+mrAyk5mMyqkU+csSSGmzjkhvOat1Y9f1PzjwZqHyG1wyRoQ7OSAhwGGhr0HsAhAlhIiasRLmb1YiBtLdv8DGdz+B6mQ0JrbzfvGOwsx/XDROGPBHdmNvt2Gyt/lB6fLfowIISDKghGkcT/ABQtBe4m0SCbdGjCiU8KLgd6hm0Msimwm2GdG/huJGHgR6g11CIh/AtB4fRd3oXWX23fxJrarvuFJlHfc9BOV+6JjlVICCCQRyeEsll6ECQkLEiVRllAgjdOpLq03HDS80FTSUUixSi5kWlrMRvDwMSENJYgltcCdYFZOGKNDH6RILQiuT5ErdCV2UJRbL54/wAkwu91+zE1ktZinIoutd+HAP0UhLASlzC8Fi++WSsUKYaLzjwL+oUaRK1ROmUKR09GoxmnfY8h8glkrqw9XxkGFRY7rF7cezM8SjEP9mNh1UPeGsRkEbECwEggkumS138Bsm39FeaO714afzNvzdt4qEyU7MQjTXNfKwYcSMIwgSEHiRLZRXSM/wBoiktGhDD2JWAqNuu6+RSUsxFlTdL3M2H3wTcl1KYaYGNKh+5rnoXM6EuglGgDnEQyJV9A6U/y5CzAaGRihIkJAlpJ3Fr9YCtdvPXdw9/6HPcbt9CRQ+U7MQjTXNfKIMBoeKQkILCJ9kUvyrtkhTWufIY0tLHhySSPYe+BbS4voXuPgap4iQuEholFwY03Xqbt1FUoa3DJKWN2ZUJJYUlTBclca+CqqDd4FhDTIBEiNEcUvyxrJ7PAaxjAnJSMlUyWv0M2/UEybTN67lu9/wCp73m7fQhoyU7MWtcZ1r9j3aargMRgQQgNGZ6G4Pdsm7xkWCxJJKq24h2x6sr7k3+LC2FgtOuZeDbcvJ9sxNSSSmglCZmjee8bvMmMVHvXoIaWagl1w04LmLmx2r0tS020P7636otdOjp7kGUxbnkH5JSkMZOameJ2ev2TxVez+HgQPEbmLEuuXl4GzSyvbvPXhu/suAMtOAqymzE2T38SKcPCePRLKu9BIXXJZt6sfWy+y4YE4NIfGuXUgpjRW+yOpNxKpDeiqxqVzH4RdZblT2G9TxSW6L5KUzfU3gkkrLnHWhBx/Ukkmm2quGZEr8H1hHzxpdCqc2vBE2SsYp6z4Y9qaayLmG7PX7Hmxk9UPFUJMtiG/wBq+xLnwldsnafc/rd/dNK2uiSXFZohlweaK21ORkAwjJq+28uYa2rYyOlv9GA2Vkn7cSC4Fl9jmhK5InJ3Wy+ynRrRUX7jhOE4STOro6G5FPqOVSScEiBvrSZvElEkl/hFUpIPzhVcPrCSapNaEemVqr80ThU5JdftBBSUmiWf2PNC1v3+RuVDRMMKIWbyIQVzeY4Mhd3wMpGy87/+AhRGJF5jgI6KUJV9PwN7NS/wX+JHLhWi0wMhOA5vwRMSP1WTa5a5fZKa92XTZnCRykuzcCq8cyGXVpfBfeEk4Scwl+5EPG5eNirehuf2LajOXjmOYqGtjnJLJ8RoUtTe6KnSOj+Gccwv2hBFFaZ89BWSoWgm6DTj4Gyc+y4f8JjS1SBr6s1xF+ctRrlRZ+R2MtNoqp34KuiW8iPTucl9iiudK+dBxYyJW9KFXmc8hwW4ZNlFwXknCcd07nr/AIQzdPSuwpIEUnyn8MoBR92vPFLAdN8NZjxKdm3Z7/8AC+F6v4GkbCV2y9TXVyGO2S/+LLSmayZWD3JkuqZI/BfQlm9m/jcVF7Pw8jvMlsYj0HorNic0VfH6Jl+WuGb2948uj+zjNNCQ2tixTmtUbvXuZYcnqsQrYftSHZuMvsSgTRZC6XcvllpWRKy/5CbTlEB8sXANMxtxHK0ZMt5aZGjGt0Q4LwI2aa0XdryEOdCVTKRZcNvnSfn4HQbr28UEL2o7eBMWS3gYxa6dS+JorC2EtEUQ3pmybR7z55chtty/+XPuGtCJVDXMky50zIS9jJp7vBkB3X6DU0ngPEQWQs3ohFLCsQKa/wCLbgnHH2Y5kXFe4gsAzgljpUKzd+hfk+i6eRSUELsL5r6K5PRmuf7qPzpb/wCe9khlYY779fMkUqu/9AyaoVxU3ors36oqjLsZeJcqdcCduIFb9fwT+d24jMVm9mIxptbxGUby6z5EK4rvfggm5+iEcPgIqscSnJ8Im6HdfqPZJb/6l717dCEQT9+yKe3Lf+juIZWxNC1b5VEmebjPvI/ur7JiSyv4Lcr5ozb7ly6DP8xlk6DM0GleLRlJz+hDqloh5g7ew1hzq6vuSUVhDDRYJN36O5Sfw/by7L9un/adyE9xZZ8Ra2/37Myv/czQ5XEmTTqJ3Z6n5M3pOozHcWY6lsDXPAzAG7KLxH9vHcsfH/wUsS7Nm99Tezf+pvY35uo23f8A7X//xAArEQACAQIEBAcBAQEBAAAAAAAAAREhMRBBUWEgcYHRMJGhscHh8EBQ8WD/2gAIAQIBAT8Q/vaK7LNLqao5C19mSh6yIbzXliNSxoj6C9liu0yymi1Q5ltn1/3bfIpDBpZsXDY8omyzMZCXUycZ8ErsxbxzjVsfuiPqELDNVci96LO2i9DW9fcVtvlQv0m4nlTW3+srlK9y4DdmmOioMYY2RLSG5UJeRm5zqWCXQjxLgi0hmpiUbhhVRNAigQPJkcoXoxObf6DcVJlcgiWo9hPPMyFdDchXU9BTCF/KvhZNUNiRbD1IcxEKlJaMgPhhaS0r/Nk5bC+WSaPYRNU2WeiQnhK9yKN4TgL+ZBmyZZD0Ik4Z3Fo7ET1qwnNV/kz6ppmT/TCdpsi3y9EQjJEYpUELpnZVYtlLOfM1YdRGZ1T97E1GSCf8m4YQBNEjSemRM0hon7L+CSVtM/8AGanwkJn5vYqmWEG6FpmQ2JDYlZbJeZD5vkI7WMavqUAEn6DXArrLJfZC+puzksxjU5Ki8kd0AuolPlOzJv4JOBaVx4QOk5EnUWmZO2aBEfy3Fhkp/wCHL2uSzI83GhDqY3GXoancgbIVWVNqVktXZGYztkv24xPgidTalFIqU982N+a8xZUl1Rc1dB3JjZ/ZGGwXiII4JCDCcIHTLUR9KakyPZ7HXUWa/wAGd57kiUM2CIXdO4hSIS4a1pzFX9sWcS9xjS5urNOhlgP1GIbuiL7Bo6ju2g1d2IoV0DiEvDWzBPw20zETaJ0ZonlDQhcs15f3MD4SJ1sZnr9EC51YhQnX2EsWQoYtvUfwvnCEFV0JXKaEGWos2NXfDRQw22NBmVCYGS8HNxY3ijQ+BcGokVxp1IZJBFvjK9fv+xqdCQ7U/dzLVhbsSoHyJYsYz0Xf+uEkiKQixWjGULJ6EqEIYpM2JgSEiMleBq8GWiCo+M0LhAy0jKyIcNEsjZdwnNV/S/shIiiotkJyoX1ENEJcDY8LeV/vRMQwSCkmRKZLUYhA8KkTyXoIiKswWWlgJeClJrcECwbhE4u+GMGPaq5MblFK+mT05ic1X87O+EilaLZCgiF9RQRCRHA8EQXafq7emFponq2NYalvQcwmMK7AqNRVG0HqOiP2+CnhxcECC6BVAi8j04J14S0IGsYEcmOy+RDDaZP4/mZ2QkWGLZDEphfUWEQlxPCQjmw67nPoqLBqWz3eFtr1NGXaj8reqJlVdXxIyeN6Q+x/0R+RUamzd/ogjjTCRi0PCDCRnliJlRoOHFYJgaxPq65MZUxBGNos9dv5EI2sh730+pHSFvvsLq4S8EeWXNvklPYo/R61w6188RCxggjiWwc1kNXjMkgMy+BYRSRZwGTA+CpjCDRAgx2UUWIY6NMmR8m/8cUu5vgQrGbEI0RHA8GNYYZwguqPgnG6SXlTCWW1iIXhuqh7E5cCQWPAYmGysOoyRTUmfn0j3gk+R9pJskurOT6i0E/MvC+f0ZyXr7YYMZAZIQGo9RabDTFO7mtH/CmU+JCmiWCyqub3GGiOCBoaEF6C/lb1c9MGg3heAwheDUiiG6l6OnhRB0FqkYNDa4g0ROdQ+xb8nQOWX1dhMpeiX2LKvn2Ec35saM35vBLhjPYL0HFkNWIqqLvsKpBLb5iY3dRoTWyn47gLe4/1mxAV8SI4TwGiCMWsDOXQ+TuKdZMgQQNk3VTMMUsOjVGnhJiZI0RDUkFwNvGipaYK5imdikE3i5wzAKlX9v2NJWj1uR1Z6uogqIYxMGNYUkhoiuQMWWGoKGijGnR2I3L+ZeM3FWV87xT75ngiByGGHgNEDQ8C80IknVvT98Y6RbNVrzQtuvgSxgs3cavyqJ9yWr7QOgJfvY/h2wE+A3izSpIQJsyFcBXYMeEbZL1yMsVHKfkSMF0YvB4hMYKMF3CxLnvkaZaYkZVH4taOwLCqwsunqJaDEFqkU8D2DKDxZKLQWVW6mq6z6DSpweFSma5Urk7DgkkXIOjJ78DzkE3JWUORhYqoqxD0uOpHhGLpNN8k6+gqVll+gVGLGEqMIJwaElXBKoVFwQIlFy3CDqDLLXElu6jFqZKfh0U0ka3FCiGhshVEmmJ3kQQ0FLTEGGhjFG0Kh+zFikLaz90XmHSmU0qLVkYJ4xhrpqXZR5DcFdRIsRkRYuFjEmjHuXSnbkOU63t1RrPZ37kuJbz+ywDfMSnCXev0MZfP6LVjYOgtYyGBLBoF1cCEyMA8IRXcbmxXPR5eHBTt32HIdUVYlSWROBKYgbQwclI8BEIMYhOqxVEJoTXLF2VKYmNYVRC9PfkW9PJ5Ps9uKS2Q3Atohd+KcWJBo+DOxkxjQ03o8ySkc6uSlqRRuYhmIOKoS4ZF1H5IQjOTqOzlNeC2GiFytsiEZrETgThQ8BKwhGBj3oxhoYxCBs+ohdbKfptwf9sp6oXnmGqyffcXAkjHpcPVOJ8DMw8gZ2xViyCeK0BDRY6BKNNXGqZMnH1s5eDKHd/A1e4YsCwTE8JYwS9QpIQuEmBvEV6ojnI2tNHA0J4F2xoUde2VydYIVqXQ/jXBFwshjW1yxm4TxtjKPFZsJxNg6YInCZMDKRDbgZR6oWPWop2nXjaWUSkzBmRaM3pkLFYMJ4CCxqXCxjwTd0XTuhVcwlsv+8LazcLoMdDqmrp7CBe6HY9iUVUSr3yXl8DcAm5cIWkxJIkISZxIpDKi8gTwDxSwILJLY0OFiH2mhwNSs1aenHUSvtH5E3XkJUllxJk4hYBiRhh4hjthTJIl5y+Dkdrua8588GJcoz+5al3wExY4EkjwTqzLjgJlGCxghInDPgjgJONCdJcyWHSh8cYmlBAmvRfPgExMkkYbGxjYx4UiHMP2Y1vVrIzEOy037cCSCYMoZcSGw27glDDUKDQHgWmBuNl2DkJYxjAzlENtxM3gVefCjSF65DtVtmxirzF4EkkkjeDGPFnG7MVEu1HMgESJKFFmt01Zf/uzfYg4GOSvQv8ACqGFcIkkemCzTi0PigJKMaF/CSMjGCCCMDFApyzJvWquGAc3sT8qKr6eCnCSScGxsfAFZs4mqp5NOt0hImzKU/v2RbAWsXlpuQjCFwvDXxl6q/JPVmZgNepOB1uFo4VAyOCfDHgkQQRiMIQTkXnSjz4ZjKkFNi2F4reLHgYhcS+rQq+S5C821Yv5vN+gkXGiw1QsK0Q4DQ0beCU8x7yoPGJwafEEEYlBm4MZjNTWTN0knipjZE5ZtkY1r5i/gY8CAcBzmeS59jVDdu/0tvBSya5kvl3OMXLwyEaeFrxASwFjbOVCCVEHEmyLIPUjHk370HpM2KU5JfwMeBCa8+25ZNzu3qXT41cJdz2MTZ9mdzML6T7szj6I1W3jZGqrt1sNhuxJLZmubrwLCbsJF4/XwUISCTIdzIh6iEhifExMc6kerSfljz2NnnPlinw2TiLRqh1RCeZWBJq3XyEFfOdipo2U+Uhh6qp3RCUHo6fXEDX0SXUV5vq9kvzNcktCgUeeT6iVJJJ/qWCYTTTZhLhF0K4lwBQnNEcqMafPZNaVigjwXg3gKWWy7t3N8l8szkauvudsEaC5OPoXke7v+HI238EJldcNSp9D2E/cWNNqRKBtXbS3eXwPNpbZFhAo0VcbXVfvMTEG137oy9hWfKf8RJOEkkjTghEGBsHjRh4JB6D1GTXuJysNgUV+UIIQNYsgjGR4g1MhIhMUWWv0UpT1fkPrKbz7fJl0De0e4nW9Azgv3M2It6lHKLzRcLBpkn78xsLCAz/Mvd5LkhKKLgYFymXOPmy5GmhcJOEkk+K2EkieIPAYkYyAbYLcNL2w5bj2IdcxiBUskoSKWBOE3gQEdhPohfImaLZ8vIzFei7iWEvWvuZN8oMwJaOqHb0YUlJHObgJAumFsLngMfzLgRIg2STgxnLhiQ3M5LLDqsj52xYPDmKkRJLQ0GhjE5hKuUJZmv3oR5qPJEPRWmfXQUUkl/Kv7SJ4pkbMWEbnTfzhNz2J6/CmS1xZwSJ4FaoW9PTuTlXW/RUQnPMtECyg1RRcpHCYT3QyqWEk/wAb/sSTxXhPPY/r++HqR7z38FjBMnokubrOMko3rAohtbqSoQ9CG0f5MYPxrD17PVvDqFk2y2IjCSSeHsEb0a9/8OCPDjB38VlQNPMF9f3w6xKLo2SSLjM2G6utvXB7ujPL9yJY6Q7x7TNRfyxjBHE8UQRwwLxzGTLYYvOPu8Oa49jlx8Mk4MYxmBCKWoq9MzQP6836C+iEvGgj+COBEEEEcTeXGuJjJxsx5Zkc2YboI6eQuKSRiMZBJOWmb/aCiaLggggggoSTwvijgIHiiOEXFJAaFXgTgsEEYGcwEDsCl7YUucjnZPwCvMSWJnDcCJOlqU/XKsanbjb44IIJCXAowbCOIhKGnBJIlx2EXU+WCNZck+xZjdPszw+bFzkc/onFRXTuQRhI2SPA58Zvo17ithzWOfJXwQCQroBSTfXL95FCKbVf7qMxOxvpnHMlkNIr5PnpBA63m3d9eBcMEvFWhweQbnxIG48sy7Dou5nnq/og8RduXHtUfmwlmOGuFq7+RWmPqNnmTwtrzS6MWDJPA8H5ar5Y8m/apEPRkHZpcEIi7JnoI1trL7IYwkMrYTvy06kbbkIKIWMiyMF4SErUgQx58d2cFi3VGT35v4GEF6yvcQktKwWioUXND8ki9vnHsIBm4S5vWuEVVay+WP7pbGWypd6fYvphL9I6t+Z68Eli5z6Ml21GseY09FXwHItpQ21UeeKnNmMbZNkGzoGwTMzPE7vQudbu9eMO9BJfwJwkhjzxyWWXUjsbZp4MKCS1H8ndZdyzC6fZoLkkfIJldYycJJKy0xs7kRtkNZ5tvDehoSFBcQ3pz7D02W8IKqLd/sxFVCRqyk455eo0ucFJtKvkWrypLfroiEM6PNwui+T3AKPc03zaHGO0KPAW18kPY5srS7TwUxZJJ3zritUamtu2e2zfhHKHQcQjct018i92xUX7oUssEycEvhujgs+6o1LyTfwZ8dPsdsvLuIrsZZ4LqaGR3LhOrJvMkYWy92NZhIkzoll+zwnjdkiN+kfJuCo86Dw2sjfx8ko2c70+/YcHy3hPLLd/C3FSMlhri3ovuMc0xXNJdH2PlAi3z6lPCbLmq6EODVkcZJcEa5vcnO0++C01LBMi15icjgbIiQsUaLLPm2FIklQjieamx9i/osk8+5pLkkfOhlfYyeBqENMgVzSZsC9X9RjLc6v3Qam9S7+MUTZ7uvIvG8l3Luz5vtBYF619xqhTq3CjGpM6L3fwQDm58v8AuNBTN0WxcPCwBbv4W4tLhLHJMvc+GWMVmWCdWWN3zr7iFJhrPJ/eM8RK0vckxUVX04UamvUabk7oaqdYKCUVl8sWMUiS1JhzUy/eQmSGWREKERxzrO1X8cSZYaaqNYxem8zblJeSN+XTll6YMvJuBNkJJeRo5MeVMeTPq6vgYo60ibdTPTyXczc+b7FLRErlRL5fDJaLz+EKC4S4Imj6y0GjTV0Gi6GuCCCQ3SiziZDLObJMyvRfPFIEqVGjqqwIkibqvJDJMPpkiPZLbCeO4N3HTll6Yz5KXSNC/j5vtBZE619ylLXJInjOnnjUqpV5fZrBFObpirFUq8repton55epWMNEW68sxKKcaXNkMb5myCMROtO61QvOlOvFbiZ8AkaA5N9y4E6rsMEhLJrUgg2Tc+VeNjezoQ4s2awJV58UeivtHaIKVPMNw+Eh/mzsz5CVAXvzF4UrV6F1+sWqW7FqsiXBS6j9l841jy+X8EBZp8v+43hbL3fwayN6L7ggkSyrPV/U8VxJDMg2tHJv4o8/rGqnChvt6iUqoggrFpbPs8WKVLbgtw6Puad6IYSaOkvGSe1Zl/BGEjCso88IIwkbAgGU9xJizX8evHbAagyimShr7DI1MtRCTCWCWEi44Rskvm/rGftUqfS3qTBbhc2i9y5S/gUVPR8jXffoiBJoZnrvmyBZUd/XDEgWbr5/RtpI+cINzNPS3A+jRLjcv+6suDJ6zexzJXt8kEFWHRfL+CmXyLTsOSGRhVjS3XdYbIqfP/mECxGXQfXl2M+9HHsfIBjOrBA1ZK1P49MJwNiJaZIa0dym2b0y8CSFsxbXrkIUWw2x1TB8JLLSRnGMggnVZoXofNsbbuQQQQIKqLJ5vXkIdYSbH3U3ODM0NLzERsSXsbptsgpAhfkki4IPmkhY7uUsyNm3IgSOUnvUiGVH7qQQTLOr90wo9VfZ69SCBWdDQsJZdbjk5Gl4KmqFstfrhgHg7LuY7Is+QtSIS8DPURSiaZEs64FODkKIaGW4IAdaO+EEcLuFki5Et6e4wTdjL7EixqhQQQTrKr91IE0jzoRjQPMMuTwjFSRnPsJe9khrDNt4QiZkaZJLB7XKY2LdZ7CbZFyX0ZGMk7qHVCuqHdPTmPmlFt+kYp6DLM/p3NC9V3G9F1Mwjq+w9ZXJT2HCdS37CUWxYQbkaBbcgaM4kl6PLwo2kbMeprO5PDVsC3gaIVhGEQtl9X+WNEJZf7rT3Lfeoui+VO5bEfOpRUJbLgl0anzxjZzceReAv5VHx2AOEnnX6IVFwkXvmo7jlteYr6fslbbTngtaRKyI8ScEHBEiEq3a/My23fISFQl4VmFoXnZlYa+wmGJxuEqThZASkugNzjJOtl8/Hg6YTHRUwTM3pU+dcHd8mWX0LukPqXIl1+jIfl9il5+S7l+TfNncbfcsuuS/qQiEQqEu3QemuygOb0yXiUs1ORkUDUaPwSiXWnpmRhBrpEvrXwEg1aTOHM+xZ1619yza6L/BYhtjTGzEBUN2JW2IVXzPQSjxFK0SmMcKrFRqO3MlE+OhLU9c8dAW68lcShR/A0Q1ZjRxpT/FGBVYDQgJl1XcqgtsyO77vxnlrq3MYKTTKh/JvhTwpy0pH3g3OCG1lSnimqHgdwRqJWYw4ZkvACisCSSfFFxGys65DXWbKPaGy+/4KRVV+4pssLr7k2BYxaXb0X3hDKWrsR6tx/C8GcR4kvLgv2Bkw8BthupDgUV0ZlwW9MRQTJ8FEj4CkYYHwkNDyWKO/MxKKL+Bo000Mh6/QgTuv3Fh0pkghYNSpsSXr6kZWencSEwl4Uk4CaUIoSyUqjkVZp9aFqC5GywIissMGw8B2NVjQYLLgqDTyE9E+qLRYEsqeAgmST4jeEqRR6iDbzeiFIwl/EwqlMY2tkSpqrbEmBI5hwVKtrt3fhtjEynJ6Iy4epV4fmP+Mx11uj5g7vS7GteVCqEPRiLxUs4zJ0aE69XwoINTUkXsT1t6VJkGfq9RDOAJ+I2RkNurm/gXlS2Klxd/yPt3IdqMEkP5P4wUF4bYjuPTMcdMRH0Gr7EK4lq+xPHs++lfMZlsPR9xmVDQ9TZ1Zr6ES6nA6NNHR88mSpdR8hVSx5Y0oXyOfznYoCOqn3FedqqDCSC0v5Fnz+4gMlMT8GRsiEObV6CJpVshnLX22/m20sO64aIafseuApCYvAdnQlmStJflCa6LV/qitNJ1P40wrhey8pKFs/jkNKoaGVZJVTb9OxeF6S/YR1dGJ/T7JFKrRXyz1wfG4UtNMjQnR9j5hIhS0H6QQeelfPBstzlZ8y+f0PkTD5rNE8UkjZELcyv0NRNiimr0+/6GKgo/ohoo31yffBbBPgnCZfJZsQVVMk/VZD1Hpl11I6JQhCCCCBbD5mX+22XeBm0udfaDQfJJD0+W8EmxWAsXkM/5zHUObegk6Fcd0hJBy7jG+JuWKfKncj05N6etiaMpjDjKTJjt+zntuQWy3XyhPheBGJVtn2Gp2EBFdlp9/wBUOIX1H9MNEEP5PvihCSSSSXOrWX7Ikl3rkkUhlrvBgjCLZLcl05/Jd/QtkG3e46l5e5fJ8kW+uZ9pPo0PRbbcV/4JUgZUpOiWgkViMFNrOPP96jLqTTGVapqS/TmkLXQm1dUKZLaBdRc5OkP1L4c1VehJGfmg6nVLsQYNCEhqSwiKtLL6/WLAFuvlCZJI3iK5sl5vsKyJbExU+37/ALHJNRkYzrIGv7BCfKeAsCSRq5Fr9DI2mb02XYVEwvcQjBNnaZkqmGrq+xJo3uVCNauiKdz7Lu+xbTrX3Eitj5l8CVRs6kQQQckJ8qkG+aPL6xhE0dHzyZLt0ef3hbdvWz8z42+45lpuhUE9V8rsITZTLZErrT6YvpfNaPBOC4MhIfJsLXUS0ttiPV1af3QD6jO0p1EOVdBKSumCizZYzUFnshKXCQlhJuCbotc/oUek2fVldIaZmpurq+PY9Pn/AMN1WvLPGCCQ27bRydD8/wDmNjBBzoNv+M0FUfP7xiUa3J58PR26Mh44zTPXtCJyT47Cm+U8CfnQhSmgUFUzZCSXm/8AAc1SiTrIWGzTRG09WQkvIn2Vu4l/9BYS1T0LuS+bEMyFpn9ESS8Boqzdp+mRLlkhc39e/FMtjOQD78FFtTdfQ5Dd1H36EBNW4PN6zRJKmT7jjTcVXyi1RdP9qTHOMkTJlsdJHyiRES/wmpoyYoCPsFzzfqErWa83jBlRCRK0NWbLVTUJKa65+FV7J6ZlVDVS75vi5kUeX/TnVHnTgamhV0zF2Kt1Vu3AwjVRMtCV0sx2BJo2yH5RkhJKi/xlKcqo3KaIautc/sQS07fkljyO5FIVCfBQjbsiUy2XIjkZnXLj2yfv/wANoWmK3A89DmbWwWvPnzHgIckh1V9XclDNtjZKCmRv/kRIjtDGbaUoljNNCEJndXElN7Z4KE8eorvy0GqS7ELS+fPj6K0VTlAuCTfGYzYdxXDRdR1ZPHNseJtQhSmgJR/lpYJRXqWM20lCeYTRFqlv3I6Ut7eYhklCEkkki3OO1eZG4+XfwPIHuhDyFEkjCUluBI0+Qu4zRHqfn2KkbbZGNIREVWLYIX+e0oZfIMkXUM6lBpLkUv2hfUi0r641HyVjZWZBIZYJJJJwSVcogi5RYhdSzSbE0lL1G1RjysyDdJZ5MSUL/UzuKsqMnXX+/ZD24Tdmi+o1AT/bDl1dfohlV+5n1pliUb7iP+whpyeaHkPmfQn2LBLGWvdxOyvX3KLNLansMM2NaOV14LClliF/tKYScINKfv2Qj8fRqDATzUzZ5MgyZzSfJizwsrmazQ2CNeYB2FH/AINsyNlGwjaGwNsgsv8A2v8A/8QALBAAAQICCgICAwEBAQAAAAAAAQARITEQIEFRYXGBkaHwscEw0UDh8VBgcP/aAAgBAQABPxD/AHnFEk9AyTzIHZEpnelFojWPKkyoV04OuV6UIz7P0u9el/d/S/spr7UH9v6X7r+lYLuikyHyz0cZMWQxuKiCYO1MqD/3zOjLH7jlFuGUe5cU/osb0ir0yXIxPUJTwIFMGTaWTUo1IYpZjvLRFHJ3WLhW7+QjTCxBSRDfGiLZvhEETH/bPS42FEwRG/bdGdUkjQJ3NYhC7xjGrlFBD1IHOKQXik0cP7ApyJP8L9r+U+0P1T7X6i+12J5X7wX1g+V0lbrjkDUQpialHLTJAoFeh4/CyZYEHDJ7W1R9ix2WOXV5Ig20tj/1xUiBLo0BYNmzlQk90/JUAAi6sMiFNI0oUjauq9wxfWauc1QFPOQy8pFeeTVMPSPQelpy+4S9oC+wryu1PReS77V5PB6K9QnyvugqoFMKtAjlHYTvmGlwj9xYuaSdiLf1RBExMN/1TKSIcAUaLpATWYTA25BADC1nLUr02QSGjjbGgowIeOULyiBXagLl55Sh06dOn/AB6pWulWy9kb9rXDX2vVI/SwaUD4qcwGaK4BCBEx0BA6gpvK6GvDws1GH9CZp/9NNARvs9y3to3LuiKc0109LlXWyCAXmID+fFJEBOnoOvFKUdDyxHIivBEPtchbbTpIVk+O4gHdRB+21x+jKHF/2TbIHcTAMQj/z7J0Y2AJOCeAWE+1cRA+KsA6AFRA6GrWA7LxmwWQSis+tD/FB6T0gWnPEP2uSZD7WpWihA9EDhKxAdQsuYW0mR4RVPY2NE/wDmygctSAOTon20kkpMV9DSqUBiBpsCAmWSHys+hBOjUBqQ3+MHoCScYlO6yyQOZUsi1BDR2IiMjMLtvUvsrIHy39R/5l35SADko05Xhz0sQS2BGKnQEU5a3sC5QC2XaEEqG+B1Vkyb/KCrTpFrl7Rn2HKvYaAJ6CvWbZHWSt46H7U1EjYoof64/NMbB3RWTstgK8c8Q+tKnUNAgTuOfoLpRYFZtSBQ+VDAsCrf5B6oIYcMldYK9hoP4oqJXbNZsGYTcwOi9TMbAhiFH/kPCAeAWLAw9F6gr3UCyhKHQORl2ky1YNZbc1wlIv2wxi4Q19ktmEyBCAvku1j3lCKxeApTF+dw4VwbF6mCxDGQMwYjWt2jfkDfKLyoUqHjevEhh6pPUfSJHV4FJkITVh1DqrkIuAzFiJCBDH/j7LygyTe0swYSFIQ3x33NiuKJbc0jVNxPPPfvRks2ew8gth16SDpBywgjiSowJmAYIPMAfa+wA9rfZhGxWvlHvOv4Yy9bdE1fPGqSQi8Rw/yApQXSyqaQ/Q6/eTotwWC23MFbCcDQL7AtKh6gHUGOq6FFsvFRDSxkBQU0fVSztIMDkbf+MMgA5KJ+1jfzRCcLWiczM0C7CuYzGLRErO0jzNIAp0LNZznYBFfzM1mLNlPuZkwysdMBldD0kwoib4DIhcp7UJHseiQMfpGLMysr6cGJDIht0ZitZehRy+5dxKwYVpkxisVl3blLIijVbhSBLCJ/MNAN+LGkrQVqQU2S7s14oLUDryTUfpkQvOYGt0QB6cgAgmQ2eaUYJLNOmRK8GYTU3whewjmxDH/iMF0TFcrZXdlHuoFBBVOW9DkU6O7w0mpxuFKwLm0tYipw8/37FGRdUa6BsJlGAQoZyLbABYIpgEoS+aQrJoq9psExLnOPAV2nHYPZQ3QMbvYrf/NdmUTortc+NBUi482LwtCE2Yf0lYp/thQ9naKxgN+XmAdOpYu7kO2UwjMQuTuau7QL3xQKadsoYtvXWgcJIBn6g3h7CviZSxA2q3/hTtAOcFPqSFg+grluKghVLoJg70rIKPGrg0KQo3AsBElTswt5NvIZKALuRckndWh5uLEZCgrmnEmc6MCG/BE3017KdQJvLg5ko3hwDuL/AASTixC+yeZ5+U/X3hLjQOFaaDfkhlzIYlIlhNBNsbRK7KVSyNAyZWct3uPjDf3OG+LgN4iFDPv1BgY0gDwI5hXg2FOd9LF2tRBBYw/4OzRuEsckxu5Bt/aAQMnVyHLROAWQeEsTZVgpsdwU6OZuC0mSGxLhrNjat8fr6E0fHYnxJ5cn9YUNIt71/wDVAPZo7JmsCcWNtxXiTDwpMcyD7A6GMjFZBQPCvAokdN4BusKMPwoTx70kWNlo9hcztlpVWxTfjnLND6V2o5+ETJsUOG+FddazLtNPPxJ/edOgRd8Itqp5oL3g7YGKn0n8yl11gKAcJmmIBHdqLDgjDDYiYj/fdRsmsixZpetZi4vJpoheUbzjfgtIOQZCygeueM4NNTsi1I5OP9hn2idnL+BtJM6Jz0O9oJq5YEHgntjcPsKcKUQObRGO6JzXBt7HXcQYCmCDulS0LZHObhkMI76B9lZWAX9ZVq0GzpEtBRmRZYeF3DMODq2ZzqMpmVCE5skMAwoGqyb5yAQz7YmyNMaDJqwQiEbQVLBoYrAHp6NwvfKGjAF8Lf1h7V6R51FBZiFHH9FeCDEEjYF4P+6UQwAIlTAwlHF9ECeoCAyMENuroSNrsj9E9Yamb6fgjMOSu5yjFpJMPJdkN/dMYEU/95sXv8nqjs1MExEcaIGih2KM64gKJXWd+0tGYZY6hCxEAU0FCY3xi1zIu4Tqfuf1YWjFatgIBsuSoroKYZkszOgsyAvfh6AnD7eewHwABdAGQgJ1ANCwwPB0fgXYJgXZIBW+l0dd6CPsMg+9HGBisvGcl4KLgVYI4Nx/2poIuLGDclWUweBecaQEAiA+KPQxU8P6KJxoHoeucgrpdmDtD6FcTGnh1lSXpWUBit0CbY2E/HwW2bG1QQENTRHNpQ9LlUHmLFrU3sQbfNebl1Avcz+wCyABshSsU9/wLrSTioBH2JDyQ6hSQbAOWSsRo8SP23LqyCkFhlDpFEvpebYk4fggyYQGqj2OYwNqsrUAyCZCW9NfquQEKZhM2gBsyqjqOxqBqvT1gXVUKBu4E6kUZuvZI0A4AYgh1u1h9LFER/2TSyTNjeSm5QFpyFwpCMLegPNxwXeOew+QCW0isjzp8emPkUaYWSXiiQDO6c1K7rZE3nFMnbDcfpZTOIeqH91xC79x/FYe9IM9xrSUc1BoayjFLhICy4Nl+8lrHxatSY1IMkWFg0t83CMr3jKFYBMCiwQOGAQ2owMEWq+pUCmogjFH65mY+A7qMK6OkXYwXRsWVAOME9Y8Tbog4SsWPNEfJMN+DBHNjP8A1yeZjED2cFv8GLyaAXlQZmvTpbUB64Gjy53wGwMc2RHYEZl9UIXCSzROX7K15dLpUOmfNdLHjRCj+gP++Nq3CPjpOrJy6wWiHlYKBspkzeOeCQudtk0cLPIJ01KjKG6AABemuETM2Hs1RvjJ5yvNBYmmgGArg9CGWnOg8RwD4GzM/vNcSXdWJLtVitBKOME6zmM6cgAIB6Nu9JI5gMbqJf6btL/oCZxOygV9UO+DF6WVAf4gBPXPpjwnhIemoeol1JiXNLyHO4LTfZA0Io4WWwewccV/ZsWpF2itwG1B5qNchr4FT5jxYHI+Fg1gFdqzmFpwXWO7susRUJDlZC99hMD5xWzIjSZhS4sf76KzSBHOIkopqgSSy/j1OgcZzCJ1RPS7rb9PgBt/FY1dFeFiyaoKrL3MY50xKMARrBX7UQAaxBgQf9IL6WykPtC3KBBRzB2XAI5JrhiN5rAFIVQPwDMwHKP7IsUAhqmwqgl0mFJRTXu1uzWefXNrEc0JawGagDLOI8hreM6047Zc4IfP4IHjCYJk4BTsqzegaDfBUg1BAPOQhpnBkbqfOB7jzVD0PAQC/QpfBAJsB0kCN1BLV8UE7S1LIXyTUFYpVOOX7InQmgCCgvH+efEMfR9oFZRISoZwLI7atgSZcK4Mmrgf9uHr4TPso/UFIkpozJZPA/MxwYMKASGdyiBFbSe5ZLmTNBRU9c+qlrGItVzh45kOB8q3MblflA5R/H6gBEg84uQvqoFoQQJC8JOilBkhAaVoajr5i54KRnF6P1TTUjx1RuVzg1GeUe7p1HnVEi36vZcKJBEPLjJgvBooqJyECzTgcTjiifykBBCf/MdTRazRPckOuogNKhZkWaf6xXBVoXYEaRqGqBQapWcO6qXBBf2gvCk7xMXWOe3JfZ6STRwLkeMCdgmE1Oh8OArWeQOn+1I0pC27/ClmvN/zqkOJ4+En5X9eDINFdxM8roCPwAC7zlSdZHhVgdQOZguVKNom7LsiD8AWL3q65rqNcuuWLugYuVXeM4CahsZwGcpsbcAkptEDGgYikI/kcPzRUqsSgQU/+YTIn1B9oH9RYSos6vi/SNbfQYDBH5IKYeHcXwiw5UvDqNEPzZDgCnooxlY6FuJoZAh5fRIeIjmvYrxiAndemj5kYdiD8Oy1ie2AKuAnMTcC0uVpQyPMXCNWgH5VvccETXhOdK1j07gWZinHiEYuDL51ExqgehHEZ84WLrf2G4roIEXZlSyzI70IskrmKgFwxb7HhemoGnxqUqgrpmNFkDRS6cbBXvXffCNBAyorpQwCjAPkEDt+LB61RCIM0P8AK+mYphAnqEFGrRy/1iuKnwLhhTmk1DUNXA1p3SSdyKGRv7gE1DuCZcxU8bISe9jjEJ43SkcLpwaTpjILxtjR79DMFdxgZBagyyYE8twWiaMd0gOTV29ApvjCt5QtjiCWzZsqzjKl5xtypPJe08U1DzgebqAo0FuUbADVRSS0UGJaQUOK9GFWPvhWSdYBcF7C00Pr75Agv+EcP2R7dAK8dU1Ddy4F+wRB9KwJBJaRg6soEHAiOnFYe0YEgwP+Rgm8IXZQzucJNbsOSVx3OBcMPmwGGqTtp9eW6dVviTVAaKm/QfTq54giNwgdV1tW6CFzHgk6GmYC8CAkCIkN3ynkcuarIuEC3YJGCerp06dPSeg/wuzkkCAtabJ8+exOcnc6KTEsHymAPsYQnExLrz1hUwo24QIsxXWUgaoFwLEq1JGo1YYHGtAFpmZI0DQa/kFfXvyRiyNDUiuItvh9SBdmpYh4hRPxaBAMsuM3oRC7BI12y5cIVOTUs7ssY9Lscz+qUGEIlxTCgJDvCbIEuxPR/wAZqIuA48r4qVQ4hN5xQTnjMm5bUowuFA1pR8QgI85wFy2BUkilx9J1YlPUiVTs05BYa20PmsgGs9J6H+Q2eqLEYLbrP2+QTN69bWYmKPNgsvo9nZahCom1PVCi4OlR4m2xqFlgLpAAvXs7rumFDCJPAiBogGPOkj3VEZqgtvOBmcAtncOsVvbQyyTZiyzfCNABpd6nBWs+RQBuyUAuvWD4F8+y4EKsBNDP11p7oqF2BiAINhQNBAT5xM3+JNE+MgFHlMzE/QJkzOKjXk4P7NCaJRqwj8JibyqMbwoisxb3h7rJD6KamP8AU40CGifhOnoP8p0WLIGaJ84RttXT7K/GEJ1kxL4q5jFEICC36+BYlXNuJjRmkysaqwMl6sBptIqQaD94iFJhAsuYi5XsMbMC3TQNEF1rgptTH0kl1BBYYjs5bhGnY1YA0uwXrowjTNIOxpCt/wCaPRJsCqI1GvA49rvQURhQuaC8CMQp7I9jZH/Ed0JgBMpnh2GC6gMBykWA/YVdb1F0178Ono2F+s+/Yo3RzBV2qr3+gcjcrEL4BFA/ivU4mDwdZFFe8MlpRUAtxUtkSvMjZ8xusHw31Cv0o3mwBNxoJ8pQGhcv3nKABbQ7PhqvovbwcqfGH2BfuD4hx5UE5Dzjm1OhiQWSA90TZRzmxzEl2uRqQT4SD7TXQWLkGlL9Bqk/bLF6B+K3gqOHoN5o2URdkgfDJNx32oL2gqfFF5KBMktqkOa7Fk+AsrwU4wrQyrbTJnRVnIP0cUEk7n030UaEGziP8IBywUEA+yelZABQFiKa/APgDCwo/LMRJJGGGMmDd0JMjBMjSC9iAxHgI265noFbqNY/gSXWIXh77FLuUwBgWScvNTnL4jGyLBegg+LKcBwc3Ac6rtD5jiPgZTcsbrSJwWdW0MZ801c2zGAdeblYXazPwo4WDf4RKa0bbqYMbUBwKhaScOPVK4w7NDSkv4nDZ1jRp+ym1V+k+UAjqz9OBabX7hBg7msKWNgdAYeyFftRftZeSuvdAXLIjwnyHmopgdiDaKOneOQeXUk3S1GkAmoOxIV+JkXeMhExbeE1YDLwUxBIUEjqZhEMgiI/wSRm3Y3QIy0L4oCH3+CAmoAJ3oFOfzQGUDwjQe6rAk4O9jhBv9E+D8f/AAaiXDURI6BXmJM5U27d2dTUCtX0JyMFGnc+QARkx4UFyg3RSenhituaknuCJeg66EP4RrhYUzcGBx7VuUF/YDUbVBkhclwBkt0zwtbBWV5hiI+l4BIhjY7rezCoCghnHzP4QfJxS8ONgNinnwuuMXJOjjVN2qdHBd9xyujBsS7CfGiKeyPWjOTxTfCmKZfXPmiiYO83pZQLg32rKHIeaKnyyN5X9YqCXT3SFXouZvoaNA5N5OA0bB3zpANZLsWkuYwFHWsWqBQSBgUgh/RTR/PJEcw7Ylcsz4k4lMv0WVaFF8SSYao1IMh8CBSyCXrsXfxYqRglQbyQBjHZkOINhX1uwizCwu8Pi6gGTJqgahq8St4uvByxw64bXypkzwwoGYF54CiNjM0my0CdpqFKTbzIx21UusgAHIiCZXDEAXGgrCgIPeCwjn4Mky5xENSnxs4SBfYgDzFlK0pJImt+EWwTty/VLXABZEI2y2gdLXGS7CAkF4x2QZFDpmDgkY0E5jDNZ5dvpiDVbcWm42mjKeGV/wBxbUmOZyu4xUSvDQtgsCxvdFauxoowWtangHy4t5a4JFBelooRwAveDKWMDt1mlASMM8mKMHBfsrAs+48GWMrwszhMyZiJmFZBwuiogxAOjRMtj7XcAK3miWwwNEAVamvwwCTEAJOJEWsDoZc7vS07VFatIuDC95g4LynaynkEUAEEXKOxH/Nq9GaEsAtKFH7ZL0wVhWxXrEpALvW7Z88AAS06m1WKbnvLbqPQsV7XetkiCFJMBLsFyGcCuDsKFfMSQPsM1gOWxFloLx5rroGTfAaZNXTDQWIFhcIvYvLW5o0pliIyJMiT2CnBGQOaL4sYgCgllsYASotjEdcMbPp8CIUgyTvwmu4aur+v58Cu/lgeBlZEMZfWCxU0j6GC9Na1kr+JeYkFYgL15RRItEjebgFpMAvBAI0Ng+oLK5PRik+OYyM7QNYImgL0KCk73AXFELl/UG86gItK2Rnh13d+gyuMjnSXDRynwk3IA1VrQj2LUGuMhjbUL1VuIHkLoCdPdCFDAquLAzcksMVCKXgDh3soMimxfyFWAIA7OFQnpqCe5Rg9qKME9PhNAydex97Ao5ZEwBE/zbsmia+gIEMBGzG1K8Y2YR/AlkiBAwP/AE0jQEEa7A2Oi26yVcHQpYhsZjqCpaULYOj+Qdg9AWFdJ9k2KQHLm8SNaCIOkx8LwbQBGPJ2OnfC2W2xGiGax1fSDRwhl98GmgQ0uvEAB2Cau/Yus3IA5+Hj4RWiA4BVudI8wQ/3TqjztuYhoJ0BydkB2ABmIld5FlTFlceOTIz3GOiwo1Q3alkXktrEmwLilAmZDOg9kwUwroydN7IK6EFgDCisgswBSITrN0AOMKEySwpZD2TYIr1wjILbALUVMT0nuyc/P1Qslomo4wLRjfmkEpcVncABkgEDU9gfkHCDUCowQ3Smnd/H7XU/2qaSYHexyqLuaJ2EvEQowcLo8XblZn4dF5XSM9cQUDoGU2k4RpTZO2IAihtIRB7XS0+GAYWK5lgnyXQOKP8AAM38VwRDEUQCUkw3bZ+WM0GKWqD/AK8hRv4IrBN5twQlygADAAQAAuAQ+ftTDAHSlcw/CF5e2oie60s+hgjUZFpN+0QtvC92jnfAbLixvRDaluBFiCLCDCps1BAHBRwHGjYDVG1mNC2aEU36ZAQBK5QgLCUcLLGsKhiS9tic1xeN6n/HoAuwBNnQ89ouhYr5J3ckzP0S2lTxqlvfuLMawixOzGZhPYswbAaNFYccNuTEq8KShOclNt7WMpkoXQ8MEB7FaZQKnDTIjEWjFeIUHNSZe/QoMoBmYDlG4rRBIocJ1jVsUahdxOEolNgVaKNm5BhBDCWhMmkDoLV9a5Q+4CE1CfCrFAHfe1Ou4cUn0mOJOp8IW7foyzUAjVQpIK6KUujRBYCqb8dQQFEtD+5I+C6NRroYrTqT+H2uIRMDulH4fdCND/awD0El7yqly6bsnEv0oE9OU9yXFytAEeesrQFRMCPSTR/IkjdCWAWlCRQsddkKG5F4tbkJrdguWZrDQU5KfOpqCYaZhU/ACdiWd3iJJRLgbFimRGHLr1gzBls6ZHBD5d/KOwblfQpo7k86gKAWiAXa1SXDGm9isCBoIs92yF4V6fjBtqE1AixPucBED3WQwXxO6P1sC3/QTRQ/vPiY1dhq5JPOMyjw1O+wBcsTbsMiRLilN0jcABaixQA250xcGM6U8R2/iDyrea3h6fGz4sIcgipJ+NHcSWSxqdi9Bzotb2uQ9bEa2hFf2MhH7jq/ij9PESsclSAu3UEB+lAV/QP+xdzwZRdkD3aamccPR+5cVHX8jR5hkyf0u9QFzdDofMt48i2WiGh/61NN0TquyFJsu4pftYJgqwO3XSsiKQazgLwVPn0x1O2u48MfS00KDMBPsTYALzAKaP7qQI3QSxHJatSCoxGIuP5MaM517RQAQJpAByV95bxm8FlJRD8jCFlSAAophMkgssFmIjkLuDjMwgVMxA2QEDigHqtygHG8sCIKRcM6NkB1Tt/m2iJBGhFGMTVhpjgAc6LCxXTD9kWI62jH9gIWX/BjEJjgY7CJHZSwQ8HL2AUIGhlInjiw4NUdauOQkXhGCl33fIMPC4Q9r3QWoOAlg7AyfVTcNsBwtYAP9lf2rALAtZ4qSXeHcK7S8fosaZE34jAh4vOnaoGkdcxyiItvyFOqrkAK1pyQOijAvYjoTcDaW4FdQQRD9L0RwR8j9Felf3uWnYm4owFTYECDiDTne7Lh0LcY2ZMB0XkGSM7GiIFdZJ4SkyPWI8jSr2HYXgZdJcV1JiX812yA9w0GcgV6cFkcG5L5BfwBEV/ZRJ81X8yRjGtoweZTemZ6Kval3h4kbKZkFc1OmB4N6tAwwpPQdFYAKRUstuMt7qmT/jP8fchTMSwKL8R+k1ksLjKU1o6/n4ubIAIaUhL+qBqi30t/+9Q4kKRsO4fAoW62TYfGw41vAyzPZqm3l0F1pHQwXwPyZaAyBYivOe9D1K8KIjf8A5Ok8sMEB2vOBERciDAMlYpvxCIrm6JPIg+aLyj8PGBHXtPkFfFxnKAow9BafFawqjbCwk5Q/wAKcNXAza4zZlDYiW4Gc1jGMbfEIt4YuF5ZDHZOgImzhOwEXbzfxmKzDzT1RpA9YNvGgD6acEZTvYefzUTqfL9LumHQO2vdrRZqGC27EVuGqHPOs9PTU8YW5S30oVB3G8l0s6jhwWebSLgE5AXfjeAXlScmv6uqVlEzo6AdRj+t2/qhgPxWIMcm7xbX0FCNeWDrBZzMFqPa7CQoMnOSvArfhLPBJZ4TvgEBvFx0MV4PiWHZX/iSQDlgm+SC7RGtAsxsMTYu4dwNK2MBhAyW/CMtM0EJW3G4yZCmpZziQO+wIOQ5rQXfxiavops8aBFiaNs0+08WHEohDnc7JwuQQGK/uPcPJk9iNYCmNiIanI8QsQ7f7ThspbSAt35qCZPo0GtwWMF2L/jp5Qsp4zkzK0tQrIdyMXTwTd8hDh5RXjKvRWzFGgawolBAQCCJEGwgppO00R/eQDpj20sKGMdxkE6r0kL5I2N6OmEtOwak1pmV/VQNn9qO9/GHy1DgaLUlzma1yl8NFs6eS0eOmcIwR3UekITOUGIunUfhdQ+EPOMpJvEQkFNKmIfRIzwV5yfiugbWiEG1VzI+lGut1N6oczrCjH7fSLnlQZpz36zK82H+r46NJZ0r231GEgLUyaXM66QCA0Xa4JqMywnQ27whZRlQ8yTJaUTlSZIncg21jCZRA54MCQAVyIzKgICW5YZlI6IiP4hTxBAyFmqbODCicFa/0+TGjGlAiGhosah+ExV0AbF8HfVP/fk+QczruqJi1YFiCjzuDGPICXgcbDQhrUI+onYuDYBRvROC4lEGo8R1PF65DclRQrsphWt1K8N+1hMhRkLMsSukOFeCoIDY3jihBW1EWyfz0HCswQ7rvSAEWuFEDEAWNRp1RDUIaBRrXG7YO4t8mNWqL+3NKLh0Vyxo42J+wQpvCrp2ngSUWhtUkalshhtWJRbx9EZJZgfIWSq0f8KDip4ga7UIwBCj/l7xygSq7A7lRi98q7TTdBgm4mJxADZC5GHkGoLsAN2RI/ObMA9fxsn2bWJ29N7BxVrRX7n7pcuSMkkppDi2Gubk1KCnVm1Sc2R1zWZTT3gJmtORLoGCApAkAEaEBAz4muwJZkHDub6AQswbfyT5KQiWAp8cpWgM0CEs9fFL8QonCSGK7WY2ihZz8wVGKbsz8qAkwA8lMRvwERQcuPQdChe3JHrc0+1MgGjq5xE1jkMQYqyXlfW3LQTt5HcB06sIVvZHxnijc+zvrxMW/FgagTXcHDlaQZoQZW0C8A0DAzcU81Y842HK9y1TvE6ToNQOij8oA0zVP+PjJdgkqVF4EAil0TP7Svgp6hOhElkS4fgnEsYSBpyfOBvLdZq0A0cDmAIkulKSazvc5G8hxS/S9noCiGe4RX6lVytRPGOQhiNlW2aqvJL1Oau4wzpwwuhKTThYZxugCmTzeuBMwWrI9ZaGDRWFtiJcLHksYwUhMQSZHvcUEs5q9HYmxknGXGi+4npRWFh5pRoLovKrob7t1yvCBQayAOYAESSTAAXle17wW25NkKGH6VsfqitReu0oXxkKJXxAnl2mjAkW/hlKmp7A9qyOIkntU3+AglkStIwMhSI5m7o+eElh2YlEIexS0iOlysTQuJqwRcm+EZYNR3KKsjeyXN+lYmwYY7C+ihuGxEWYDcRIiwp/iCbnz8okDRXUOEoGabJ8SZXmtNwutsa1WZEWgYZ0gn+GYEBoNI0k0wuLvAQ2JNSn2WGCU3FIkybHt5xYkww24OymYtT8ULHo3oZtG1J4WgMRkfRZ8gwEtaESowzhE3q4M8Jik9g80WHbKwbiGfx4GJ/qK5PHMMHC20hMgKECsp2+0ENMyeCyioThyGXUYf3IV5JNfkwVoIe30DkpVKbRCbKcyPIfN+hJTgFFeJMaXdFi/iwFT0dcr0CwBdJSRLYCyK9wCeNAFfZyWB5JNgESUcNDiODGzc3r7wBXW7vqITxM1fQgEuoKaBFcvJWfgupCMBeV9nkfVB7RqK3gXj+EhAyItZjoaSmAEcn5TOU09uCi3mOm3iOxXmNRdF37fECIbYTuTZafsEVaOcSabagnzWIlbDOgTBTUGqHoxDT+DBI3IcJt8IieYviHxKzAGxe/ttS9XhFPQf5QAED1A5JHrlISuLFFhOJkMdqmgJJmZWBt6GsiNa3/AJvFC3uEy+NMzMtuUNmQLuae65cj3gEaq0uMUYELQQItCsZb0Q3GYisWMOwTc/2gp4K8kCr4JGaK6Q2wQu0DKcivmLrDRvMMWOWCcwvvqFhWWBdqqORJE2LrNWsCn7IZwYhGPjwdctsNMeV9yuU3V/Xdf0ekKsYDxHAHZnXOBxHh2tANR1GwLveejSLJ3VpJALytOCyM+YM8Aphek+W5zgTJX2BPN5WjBfbDPb1ojWBT0KFNOn2uZ1KMUytqQOF7RAY/gnTausD2rAAUaI4zOPaP4ia6fQuIhPEQMAiz2PySl9AdDOGXaRL0A7rez97M9h0RqvUA72Y+adO44tVj1gZgAGI09677PT+87Rx8QLHituRjFFcJq4wOAPgD0I1/uAQ5Zb8AJvm9CBQx8AFhklXIXloojO+QDoDWR70xISC0lZ8AlYqnOMuLjsvaC6coAwuLqGQasJPE/Yos6455u67LxKZTOGB8xbZsgsqE1HbBqbFZviDhpm6KE3BtiQsECFiGlNWzZXjFWMVerhEYrXTrZONL9jFC+KuYxIzJNpKlUzYfJchuuLsCE/pbcv2ZgwWsE5pd3kFgW93dY3ZwRi39Ui55Ww1BHOyp1YBxqAPUytYQzxuLPI0lIGpN3Fzo6yyZtjhjM2rTJ+9PEAug+8rse31KVEgAFKkCpDT1L8BxIRgL19hEfVD2jcVugxFH8q/bhRUfwV6kMUSifjSTVlg3lw7DEbagTvRjzehC8fAZPAq4MwIkVmPFJwlyRdFcX9ogEBqhEGKfSQH5RPK9SrhCpOozYAioUNPrR8FwJMrCAMqoNXih4uQ8blgt5xFQPWRlNIVpWzDpNR7pLmIspLJ6NqN2gG6BNaITZ8ajVfRIRWr+ILUYEKV3bbwjQNjIs9GnTmA2drKWYX1ANKZiAT6DfsAYQUW+g3xpGOxTZ7kjM2NIWMGzedYAuQIspMLbYGxHtWIE8o7xd6EJA0vecQVNKW8IpGSjDBYs8eVypkwtWlRYeozQwKuM4Bh5hQ2mpsyWbAtlKCdHX7v9BrSeJCFI+Qj4l7+rWkQpT89wEAu5v7qID+n8CfQf/uo1KGOi1R7GyKF5s4j+pqAt87gIWc+wKyKIkkkcuNv8z/8AUvnU4EmohIAFtU5rO9xAPzo9/CMr0e0RenJncWXEXj9j4QFetgHdS/pyOLu0FhEV+10TKe0vWgPQ3kiMqkA28qHcoBzSQMmdDCYBg9yFaYtRSwHkli1xsC/rOrTyyj01OhqL0FBXoockB4NKFPWDDVWWZyf0CvCEJzuhPo5G6u4xM6w4iqFmWXugdwnCFt9oDyGYR2onlLoYktAxUE5uyMXTAo8XYGZgniDRlTTSbUYA0rayHGhEmSbFXsQZ2C3dfqGH0nIm+d6KJUtALs0Lzlib4nUODkF+Ha8EkyWzkgU0E+eUzYmSVq2g9XFupoPdr0KAAiSVDOMmSJN9LEVZ31TJ1EGbqBFg7MYrUAaF5ATt5qhJGgdhSpvPP41M32D+Xrw/zevIcRM8ziva/X7qW1eZiuBbEbA3CsNoo1SlAKcnSV4FDQiHnQKaHyWoHLWlQPNCubIKaMzxQCfKEleJmj+fa+3IDSiQW3gKeiNGnjn3EsHRxETktKt2I0JjJj1twLsfhv5MgLomZT/nDiZR3ILtyVB26XCFncIkLh80YXLhqBp2LWVKygWYwziRjUgYHPOQyIhZMlW6CH2XPS/ceVjXjBjMAyR75tlicApRxHQT1mnR+AB1qj5QP9OpYFIwpzyGr37o+xeUqQZIUavjvGYRpgiHVpgi1BARGS1YNtsK4HzxWcglQ1ENWIbaKVCvkc0cS1C1PHkZeO4UBgM3yLTKYAzCQdDa5l0dzMivop1WP0WQkORdQIAGpDDeqRZXthmAhZG66C+i6ByFgFCXVrAlEmEEyUGWlYtnLKFyhRFLv2e6TMvW/iwkCnv1V+JV8DPGDDIlmQrEU9gHogtf7bIfAmTCa6/Zb7RtuCyOdlv6U+8gLrRNxwVvC1UCNbgwUrkQcpWIi+iS2H5QhpgOCM/pQZgoPad2WwrCAq3RT8fkUgVY8lAOzIC4bDupbmaGOiKdrxss2MUw+iD0AjWegByAhmXYVeUXlHjKJ8QkV/UWwMDNcoFvzhsmFDCAtyk5tOzQYrOVYyn0ZWfxMQGAFjUjaaTBIZyoAjXkpT0h5z3VZqW0Ae0hUapWiIoNE2N+mquw7ztcAPNDH7PIkVnZk2AbUU6UC5O92Ii3MUly7T9s1JIFiL6oc+K0jZJELkuQCIvbQYvnjDDYg45ytHRwIHsaDWhQYNxbLVMiTicUYkm8KCwTihm3I/S4QozAqAmz9yAwRcN1iTLVcxWCP1YGI6AFqH0mRkHsITFIXtu8VzQ3FAFM2hYE8ag2NpGMZFWp05C54Cz4pk7eDOscCiUzBLhLoJwmIDSl7hbqaTJfkU25zgTJAV4CWYlyyFgFAAP2hs6etbzwXawDow+Jc4/fmlROI4J9TuhOsSLEFvlnrOk+zQGBxBTh4D1/x1BVXfKH9AFY5RvapkfM8EGdKK0lZKHIWHHwWG/in2HgLsH28xqCrgyuE2J6lDVEAnnkSwMCpGcE03RloI6o5ZhITBBiDgajf6qi+qgDo1AOnpVQxIGwLmKDs6ZXVlsLF/6XbUSCeCV1IZEU1IjhYTh4NKlEcemNVIwxXjzJcQvDMWq6UFIMi9xjYFxiBRHcBp58BGN7rWQNXd3x1vgBjYv2K4dKK9yDJ70k0T/AOFptZLRGCqNu7GxTiX23USYi5dWnrGESxgvXLPWF8C4K/G1yMyLsVePv91E43hLacbXnmMWSSjHr1zK4IqKyVtK5WrAeIsSwpR0LzPYuoa/ZeWyabcUJl1AioNYK7OCQCbGQxNmH8SaQ5yZkkr+5C2CXKQ4IKU3g+kysOlh0fb42+xiZLKysyigHuE3AZ/fySS0DMqA8A6BMvEcL9nx8CAH/AApFt/5uf5ViiE7MQ5WJ7i1Q+VxXOQcp+9cXvidKN/6GLWAXhndYMmQqjnLL2cELQLJqUW36IZgRIkJ43aHu75BzasnZMXyqEmOIDTf4t0B7+IJ6IUYK7hwIcrcE60PSE0eM72h97FP62xOLXxUuJ5ZmMacoZ2Nh0K8ta4jA1H2QkvDTcYWGjoJmyEuXimKFxKlwJWHg9i0RtMV0bRFbCADcXDjVd6aLTBENZr+I5KIKwzV1IMNZNBMWmrEKhyyA8gwUO0Jz7DiS/hTjTOTII9s/AUUxdk1pVnkcpzIYxIuX6TuVsL1oQJXYk2WxL5csRgJOs2ruAGPveuxrGXAXlGdtRkDjFdAoNin2qZ18iydUJC0qOFLV2jdZ8BqimhLNFAGPJJ19PjybIGLXfHK6d2XCuQFiAFOCe3VYesADp/xF/lkacVMNTMIEzP7JpFidZ5ChJkxVi1ha3JGwIPvLrA+yv2oIMgkK4AgLoEtdNsFGIntp5HYwl1Z51oPT0GxHjvPABYkRBBgynnmIfSNibIQWABJPUAGkPTvy4OHWS2Iiw0BivDGhkJBCtzmJ5Ivsrab2EAgrIF07zvB4RoJFT6x9LvBaRUaUtNqCzDlrYbzQb5hQ3CmxML5wTnw4QmX5m0TBcUZ8H3cOE7kGItDUv5UOYTWjSPIcIWI54G5m2unv7erQ6HUkQen2b09EJ0mkUJpIFo+mmQ5mKdHAQT0GxPoDbsQjOQzCzxbaGXdMtWXtfANwxFZ+/wDICOchipVe6gIqxK2qsRw6RtR7hsvDdZ+qEDk1GqGoZNTP2gNYoKaINinGi5zIj4nCF9vBfRBQmFwH7ph6p/gADf8AHVq+QXqsIhILsREp3RemAoBwYqccXaWqFSV5i5Nyh8fssMbVJ227GTbdJTqCrTxBCt+xc/IR35a4JwAuoG4ymLWxYMSLOphqBpGZmN9AzQn4Z/DGKKTkUlkjYQOorZHYQZFPQSgTo3LjGizlYtOBc0iQKPZBee63SoOugDmhvWRBO5jELreFRMQyKtiQRMTVcisFaTKAF+ANVY4ofnZIWqUytTYh/toglwell+ziTFgkC0tGvLTLdco5fI9iTMgAvpnaFzbkVYwCMgzK/vPgD+NALSEQwDvwWkgcL1ezxsegFAWCsP8AhoWMormvV8oN3H/iGLUfCWaJkQyjohJNgCnVB0NMmxW80jHt1lJk1BqwrU9IOJZFXApcJC7yRwn+F2xyrXW8OoBw7A1HlKYgOPNMPUeoA/xgKLmrdnqgeoIqWOcBwGcoM36sYB3Cc1TH9gGwgIzw/fiaYh2OVJKOQGcX5HXUxZeM8+pIvJ7N30PUyJFJniT8Y5XM8+S7KBlG6D9ZYzW7sOfzIDnmQxClti4gxvZqoxYSDFttkWIupg1AjKAc8XXYngYoUyJTR/oPNdbeNVPaKAiVDo0mFUCcPzLnhY9wnBSSAXntob8g2NXgvumvpfbBZLEL10e7avbUDNVhucAREzYWGHii/wA8huaVkebSDogMwrPSV24ngBmAlI498LTAy4pgD/4Yce3GONHLg5ImINFpl2AYxaY8kxALPFEXDEtYkhTlGBIDdsgKwBfDylAFyOeOGaQdIFew46VwCW9e60wYAbiesGIVKG5Q+IwJ0dSwB3xdBgWpyfsEhsALzALzDjBzP7lzkAP4IB/1vSHrQE9Wook7C4/D2iaaDzRIaGEpakDKgOPiAUH+MBVvVWOnoHqD1VLQjR0VE7vasc23FC1EzvPAECAxBBsoiRIOmvtVDx0BCs4wdjPqCY9k/m9ZMrl1ZRkefBv/AEDcLkKwVa/hugOFrl6+tDrWYNuG4YueCukzJZ4sDFdiy+jvAoxw2FaQtBbMWtRFNS0bc5wg1GzHshmmNjLFWdd84WPm6xS53Bi4WqhearGFRMDwAjvQ6HlVeghVtk4IHLpxhoo/7IyChUVgTIt3241ogIBDszRd+sXnKh8bbCyofUF23Al4FHIJp8MTdEuTjAsZMVKSKOiDpmRKVheUkFtgdI0l2astJe5eTEOpORYQL2BAocGt/E3cSGZ0cPa0uEA1xMiZlM4CwmIxCGUK7wpGP6kDaEE8FDJagFQqIuJjgjYLVwvZB0zSKPUTILouNEz5ILsi7y+iZAVYoQ9YwECsDfKNbmwhDMLK5XRhR0oHZhcfADlX6R1lwyCkk32Dw/wAPUPUB09C6ei6elvUl/kB6MbSplx4joLITiWyzmOKmAvHgz8oR/VX0vkRb+myznjJHWWFDIJMGqjEE2HlbCQ9wJWIiVlwFzXfGGme6YJDcKDztrL0toKN2WE2XUzEQVjlNsRzAWFxZUGBXYERGBFWP2Lc2JKkTTpAb3oUVpceZidsg8PgEGJnEIYL+95AF4z3iXwdW7CpI9oyWStwSpTxVzI5q6FpmgPkEUeEcoEYBJaWKZQEBcXyRywUgQH9RJLqkdI51omxRRQHX/RbW4mXmblWBw0ZLP7pcylakyHsgrhcsFs1C4xj+ko8ZH8ajYZgqD4ujZr80xkr0NuOHpEaBSxrp6Q8DessW0HS0aMq14gawByKA/kClV0LV070WrJIblukeexsEuPgPIHNSyaGCGQDUEvqOaYD54H+AAHT0b1QdPSegEKk0IQDuORdDTHZPsDcLCjehaxTwLr1fRRzehMAQAQYLyZlmYrQYG8TKZwUoO0h9ALlZ3jin1d8R0aj9qh/BWPxtLeqAeefNNmxcl1lRg8OqgO2LYwhmwTd/wAWETNPBBH4j3gPh4YmlAujyFhAlar53qU2gvMWUFgB9riCKIE8KVSzlsDZCQGKDlB1QZnEYhtIBcly4MBjA6SpTTizrCYlGfRgFSZZmEppNw7lOCbslG2Lqq2Z0zYObaNjX6ZeNofG9iyf4XCdsDFOjHapM3AARvLq4NmfOEy2YTCsAlh/r4HioBZtJsie60wkQjcIUfwCa8fD0drVhVnOjLhkCswEdJ8OmCa7vmT9aA9rBAsBynqI2wfgR0E9QeoHoH+EVePa1noELeg4EfQD4MundmgLjDMr4U6v8RJHPGL/AKdXIuNHiiTiai/xgplx1ZjTqLXUwmARei5AwcUFdczo7rRLpHlfZQIF5KPSdMJR/BBIBoBqfS9U9kKzqLrPLCsV0IsB/KtgR5hPUZcbUoeV95epBnI4J2lR5ouxCxgXvgqXxBMBqaLlqQLxvoDnAw5dyF7AJ/i+vGVp+lixu4l1hQol83FvC3EmzvCVQ2lgnaG9eI2rUwMu/kWA3K4RL9dW2iGA4IRkNCxX04+CpGp8AyFYpx8fkQlZ2cXdCsUVz24FMUAuY4SGUMuJMCrK114ei0oWK9VNnZzSBPyD/KAA0PXOhU0VMnEua8hO9LmUmtIuYOuk/wBCWKEV3fmO2fLBGoHX8MWyMQnuBUkmPzPfEC+BFn0WQEN8XgLS+32EOz8OlYYu1dRcA8npyaEHyT3XaLJGxGqH69t7BHoEdFlXJbQRmbywFqubGAHy2ktGgIJZp2IlJdrFB0gbiCF1uIgxsJCE5WU1pFg+MJhleuhnqIgiBBgQv3V0oI0ZXFfSILk8iYQyXkJ8ZqFYoOud28UR2KcRuFfOZfWDKhQbQWLffEdqGyeGZ+jDEUGqN8AJh8mixSXpgQvdwrCT7i1lkJYgBNchJGwPiq6ByMUBnAysjF7os2J9w8tVh/AA9B09YD1hWsp+ty7XYDcyCDUIfW9iwboMn3kct5/RFq0jq2AEgMBRFB+JqWXw5o9SGXb2I7ZVA9ZfMZaBQYayAHSYEs0zWvtXlTH6Pw9R0ki23VYsLAZWtptwYnag9Q1B3tITS+oIjAKJRiYIAJGIIhZC/UI3oPVGKz1CN7l5ciUx7RtHHGUkLmIBfwaMhC6K/WZ+4ASalFjGGscwmAw/FAUSJAfBYjWZTPlqGFTF7y4mOVfsmrRoMiB3AiB9Q1VkwYKXTYlXBjYrF0Mj03w03zgPSFBrAhQGl8h4154AsFpgoSBfg0nYA2AU4zwscGNxTlJFYan9cFQDUHzc4zVDhlujkTPe6NUcooBgQRIg3hXXWa2AtAaEb1NXDvvqgO4mtQSEao/hYA9DWkOksk/NxgAAEPnBauhAdiLdyQhJX8xYkzUESAdB2I8qwsIAzJ0MTVMeJCXZvaw2CwWKwMrJAeK2PxD/ABAFUuM/0qBgxBSM1VMGQhEBYLZruqIo+F1ccuCxkrwMSmkJIuS98lfRBHtcs57K6tc+1Zzo8VzVwPBVnFe8C2pxzLet9ilIKgM4NJAK+esDEp/OVog2J+5ya2bQqvQTZKinAKKvHdsTscuukWAKCThA1gvbiXIJbdTBR5oR5I8d35cBIg5/gBIP5I/BH5wNQ/xGpH4BYwiwGsGdaBCtiqzGRIWMYPhZrX2nQGRTAdicC1SSOVkpBfw2AuoI1iQj4Q35ID8HEz2CZBBZNTAkwhqQDAMLESxvuUF7hQHg78yjkzg+V5wTaC0b3MzVuXiHiiTH7VX7dRXjunN5+Ei0EDfBfvuYPKa+0hifpE7fDxguLXQUaTPQQfgcKz4Ni+QbCAVhAl4WIKc3TrIVgvH479EqU+K+h3BVqjT96sDecaZ+R/ywESCkUB8SETYwqgKs4lJSZD4wAVcgsDZZJI7K21n29XrRgHig8xgpjwPlosBSPQ3xHqiuekFUeBrsUgL1hKSDzheV2kvqsPt8Cwsc2LwFdgiroiEN6BChi/gcZrFWb8/kegXUGV3z6AmWsTIQ9K1fr4R4Cub5UvpZinvCIY95j0m4zQWieQJqG+AfHKIw3MDBabeWU2BZYbFE2uy/3oWzCP8AkgIUgrUUo17lxTKlIZkNEfGEpcFzZxpCgxHizL91BcBin1cATmYmg7if2fBKJNaJreNSNXGrDiLFdmRCJWhApq8855KRXfepGdIiGwwPas4wNmQFW0ffeUF3ZGSmtyD6WoTp11FGAmU/2LRilnaDFH+4w/0ULubHkBMfCbq+KAniG+j5xnEdzBdnlhihlQwJ5aEUvYPU73LFemw8FIJh2O5B6+CSQCQgi1WQE6jtAZFOCCbCXGsFsxCtP+Eeq/8AhgrZB1DWgjASyGnkmm/DZ8IWrx2zk9I0lFT5xfoFAoP2DZfr52whlHQNTnoD4Jqjj+6fnvIKU2AZQ9cO17VTiN32nWXhdYjRdb4IknAJEXcCvLcJtkWv5lTeYrBoGRfa83C6ZKsZhDel3MwsEqs5Z/VPa3nJ9PhVSEv8ZRadAgXQDJeys+4sGOYVxynPCOaj6EuSDvOzl0uXt/WCMHLfj/QCepCeg40istiibn6DQJGCzbg7K1GVocGgfjD/AIZm2K8Fq+I9KIMWc5LVEF5Cupk7AucEexf3Ru7ZNQPxRPADIQpQ2kjPLzU1xXnagmo4waRQsQQLUCAHfYESslNFuWe0Ua1WCUaHNbPH4cxIUhWHcfb2u2+FBSBowYNg6roOgAkBSEGAJYD0RmGke/eFwSj0gS0Vt4LjEBkPtMmB9WclDItHjazFUb6q50itix/1Fin3Ab+kY/AL1iWcNkHDlKydziVp07Dlk8qvPRxRG/GFCB2kjja6HpCQHnIB2CFV6WDeiNrrT4XnwZl5F0sSSGwOjQyNLp06f5jK3Il6MKMiD1OutzquwDwLngHmu8AXArtZdzI3GR0CKhMvRCWAaA4ohREKiFyM7jnemahtmXOBG7qmjKApsD2IrlWHxINktOHwqffYT3IVhYAzhFmQIVlBTegN7WgFkIv3DMn8TROEu2QzugCp7P4QtMngQJBeFuycX4fowSCcuKc5JKvwXoXQYIRJ7JBy8IwH3geQuoU0Ay2dgUGb3ZseVJ8CejKf4v3hU453l3lWvStbgPKvoz1scVMjLVBXsTZDLW+64wrAluz2t59CJHJaXZ6YeExSCY9tK9B06egYDUI7eAL+UDso6V4rEMx9K+pRBPVFPcyL1oTGbFeTD2n7XttN34AZn1tN4QoKYuaNkiGZCZIH7rSuhYWAXQVCMm+Qz50xC8Wcen4tT8yDYXOrNhP2DRDFZRrqPKlQO2fKVCuObwD+XFQtlkMQweqZWIBNWPuDaKOZkJC0rljg+WQvisTsME3OBOvShvAunASmRHFxZ0Ht1oLPNmRvO95OUyIO3f7KwhwwdkSTarZK6XDNd6A5rpGYFb5ZsmKu8B2uFp0WAwq3RARhYt3Ys6nRczQjh340tRT3rsgWelsSrHJwApHT0noehGzO2EsCius2N6O+15pRoNeBTACd00fBCnvFA3QC4L+Ad15hNwnuh+CiST9E8aD7l/DxmfJVEZA62sAV7da7Gwd+h1DIIAQAASAFgwqB879IiXhGlX+QKkafk+wGNq0gUOUGbgPtX0dyJmjipuS0hThxzKwGZTyhWdz4GgTFjyflj86TQFTTf12bIGgUIUV4zSeiAwZRpWJzgJ5pjhNH9cleNh51JR2z0rnKV/VEPYQRDtGJXR1uuOcT9p1k/RvktBrKrIABFRO5O9BBPhYv/AuQVxgiNihAJPaIr2FzezGhOnrP8QCgooh/FhdEDeQutexTqYt8ugN3go8mhuOLwJZ4k9FzRQneJN0asQbBa1MgRoBpxf0tnERCtWFWFmxEwbRkaQKY7yKAAXkq9bO3uXcMJCNQb4EvQoK81D/B5kAHTor3rPx3AhLDi7pHqnrCA/aAUkwZ3IFRtuJoZNeEJoUFcYLVHzyIQmkB/cKEU+yUEQnICKIXiwBvOBisPZRSLH1Iy3RmKFuf1WMS5wl7V9YDix7L1xCc0Hk9Eq1UFWmw3wlteVA7i0+LRBH0IovhFda0RgugI4KVR3RBOruZOgXXO4K91PEhiBmLaUNkjdQlhBbG4CUK6w1hUQRGqD8MGr4j7TGmFxswwfRFmbVY1E4pceSrIXoOpNjsiVpCFbtWxajgkrpAqQBdohQscLpBXGJAxNgDoMjxIELirN+0tkcCBCkEPlhv3XRoCnqEBoOgdkNbf2nMviQLqXBUHrrEGXAOBDhBSKz51COAgQ5FHkus8ADordoWm8scFZl++Q8AFmuo2plOvLDaCer0ZcQoyXgGJWFhS1rWdVBoPQPQf5gCnhwgyfhDRlVwyeBZ4nYy7hUgAlHnKAJJ0AU6fev7UGiMhJTTGqj4ySAN8IcmiO1hbwmSd5Ojk9WGeqswIkmQCD75PMpnWSLSo9D3iaAGmHRTJ0b7l0gVWFnIaQASfC8c4TkDQMQoCFA+QAOgOnrAC7sHWg1DK+O1GcfKrF3DxNeEVbAFQSaS9Taj5Bg6CCiBgk8nudcAOV2vMoG5TvaY3tniXVIlpBqsb9fCwaMjQa5wYBMkmS/X9OJlm/aLHeLBeSYk16n4WqEdGCuCJIoj+pgiA7UDCE2g/wCImIOF0Ay9HBUGwV0gTgUml10q1hJswLIThB7yiea+jSVIe1lczQI1Q5DHziMtPkEIoQQA6N2ZwWMRvJFdBNohKdRwLgwmwBBg9YazD7PSzWj0DlTdWQa7r4JvU9lIVHpE8wfY9AK0Axb9x7ZEWlAfgADR6Hrg9moruSVGwiadGp3kgu+EVEao/wALUaqoiXMHwABYxKB24zm+wM11+5pZgwYL+NVETAbLGoXhxL2RFxCC7UCis2NHb4HgLBcpM21wKEE9Qd6sHZgbguIQDchqCGjqS5Su4D7U6UqtNeEicey7U1eC7GOcpjNkvSJh2QBsFkiLpHcUCwjYveGBYKGBqhaKbez5vgPBZ4aEkVOAivyeynWWAvGxZxAAbj1OqKBC8cGAcBatXaS7idYIuCeUzbeXTwTCK2BWeBZnpvOiv53QTcEKxfGwtNgKgZb+J3bGs8wBI/4tzAK8BVkC8BlzyUIfCAeuAD0PXNh+V1yxBZW5C+kZUA2R5EEWJB5UxvwRhPEYlkZc0AcskGJAAXkllGIXzmRQdpjgLVYbdvZgPBCl9mmEeU+/ZSTBRyTzIHc8Fa5ordOLNCRs/ri08C1NsL2tyIBonYW8vXuXOYT5WCfc/DlwvfWsjI0RpS1CA4C0wW/RDwC6U+iS6ULWqKSV27vGUMl7gTBrCDYLMVEBqxDsTG2Aiug7oEqEF6BRCyEs2d8A2DU15KyhbuLX9ah7TeX4M3WrAwXRz+iietzgoVVA3hRYO8yZI60UADs1zLVWo168hO/xHwzaRGiIe1k1mYQ8uawAS0bfHelSEPiAFB09Qek0/dYpeCjaKDKjzvKgXdtfAuVI/NIAlFOj0zMIDngw3lahjAa31YZR4YLxAMC4WgOY9AUApdhzzPXV/C/gZXTfsOaYFdG2obwzEsxsgch/BdSMUXqlmtBqJVIOZLRwt4XZagZVwUU38wBVHI0WYAHJ2C4iRkkDQMEVPAD7oGqgFLTRmlfAm3gaWPDVLECMRNmpYJ8ZU8JRJ3RSzFmZLbM+AO8qpAayOko/C5ZKChKcjEKiAA6Z07YZXPDUSrgVBQPlACh0JKjgfCGjheZOg6tpGnZvhiUj8Qqk3DDBLn4XoaGzoS8mfJc9ATDtcAgHA+U9KW9R20BsjnQhsUqoHKZHbsEPVVk5gU/ZJ/dfbvdMLB08A3ABVcyH+IcgR0pGssn+3qq5nbgx2as2qchKMUao1B6IjHXyWEJCZsTgidU2ZYkknWrvgU8LAKHlM/LomcOqHeqFexg2IyPxcWLj6yR1pI6ehuK4l0h+UCHxkBoWV0l1HCoNGqfJR2t6qRP848QSFF/Z3EiizTfGspZZK3AnesDTEJtBCM3Kzis2ySZHcMt0ZDzTvJVxTuUVBLqI644XalmIiok1YPYwv2GFsRNB9tfQm5zStT2EMnPB8oqFzorwEM0+dCNnKvxQHK1yCQI1C5qvAVb3BgdmYka7lp4rvQKcJbkBuAC6v9di04JzsZXLRfIF1Nc5XBEOkABWEWpWQA8JGrouQjzqwABARnEOxIOnxUe7M+QCdPQZ0Jxq4+/w7j5g+RKAaZugEEFsekfokdTxBoyh8hGiGDzXkLaCewPgoQCyB1G4ak3w0XCvsDbVbxMmCjSGFCCVi72RbEhT5WQ2ILtBVM0B6HoeqeM68dGbFZXdQhagfAZC1brcCsHGQpesKHC7DzYED4CQFQPXIAEH57Xh4HERGKKZoCjBIhBinQiKCdPQDz0y4BEnZ1stIGTNGfDqFaQEMO1iAAHAqv0J5gvHRrbakdao+YX21WFFSsgvlZZ8CC6raRod6l6HYU7e/wAFHXP1YZNACn7BQNiXOYKO4dUmNR/KBFTYLak18yDSA4RNUES2Vm18UN0UaNtFw3HoCYcrgEA4FXfnFojcss6U0YuUHyrfGNR6JkWVcB4aAJJ2BVkBmxwA+B5QcYfFgaVRy5ssABydguKFZZDQMKPMfs4B+FgYASJBt6jHTeCYXC97Wx3JMeAABAAQAlQxTWQwHlNbsZmyOpBNU3cA1zWxAJp8Q334PA0QO5vX6wgquEYwIFgakrXEJFE8/gHD/B3LjUF3YgWWWhm87EN7RlR23To6CsFE/KTdIjGoQ0QNRGA8vANuogF5rR+CQxaAqJTn1GRbBZvm2bheB6TEyqhVB33wcQFYnH9teDJZSNqE7kesTqaZwpsxS8oKQ7SvD5F8Ih06l2wPjbKhxIrSiMgDoCAqzCcOmXwCa8gm8PghHHEKAIADIAVUAAbgjn8ATSkfu/tH9aekeh0U9J6AXUdMyuJC2pALDtVXxNhGROSeFdtrjQOqnBl3MximIVpB2TS847UJKSGMUgXC2YSbwI86H29o09mkDYJiRfIIdwh2OkC4QL5RMVICAFIPRPCVmXeQh3Or4BUPwuBJvg2Uu4IGmGTaR32YQXYJoo/gAZKrMa0I9lAEP3OUc/AOJ7KFJ+jt23J3Le2cegWPZtysFwB6RvMvUCVpRRqcF7CWlHIagUDEBBcQPmRwWIF6y4P5Xhk+xAK7CYBeaE9DbAvUAE1EFEbhsgqlOYCHgZbmsNx6f25+AGOzqFOHLShvSOpRD2vtQ0LQhcuHKcUexDgUD0GmED1zB2GkACXJN1eQ2prL0DmyHkYJwFW45NXBeYy7o4rFRyq+xMYyrJARBYQxCyqhOxYuoikwrvYugF5ZNIFqHqgWfHaDqCMGWOA2AhhAhuaoqOnT1GzfMaLomF2AYUisQHk/pGi7j7hR+41Eon485uWTbJ+b7C9SOTeYSdMTNCYVgC9BMiXyaQZbk5pr3Q3IXsX6rDY5UM8QEsG9kAAcD8ACiYTs7sBUHXCSPExKwIYYBoC9wPyvg+FpSDQ7CsnBkjO/sWUD1EcFYcNYV0PfAA+PAp0G8e0PC3hSRBBeYvTaLFKyCxlczVp9zagIExBVukSmv/shG+iKh4mlxPhIGu9QgDRGvGK4CJME52fIli9pCxin+I1Y4w+EoDCpM3ZsDUQEI8ye5Pgowowk+CKJw2FfxHy/ChQm6alfFByJNIXBzYpBZHK8AI/LnDlHnwE7aTht8F80ax3SmGAn4EkBHcKCNYZuiK8N2ADngLj8iRsBgnTJ4ZRPCUAN1ymRBdq4p6n3I1qLwnK2fNPwWg0uW49UbkpYxBLYLyz1vtR2T34dC5GpTeisitNHK2kC1KerqwQ5sWGiUbeDkRrJrIld2asIP1JY9RQORaLDCqPZe+cvwqAHgmB9q5mu+IsgZqO8Rfe0zIWAfCPmWFiPgoAkRgNbjRrgCPZQ4AKY9mcK0EtBLNc3j8LI3yDvb9/4Fm7AbymU9Yg5HciwWBgixLME+ItxhYtchovl4Zp6V6gbmG3tkNBQowCuwiB6D7SLsL16SBMyLZ6kyv2ogBdKHWu4T+5eyKWoxvqBoteMTYECsQv8ynI6k6xqLrc2HZEAFAE4lgAgOKv8JHi4EWlN3Sci9wVfuHquwBzwnsvytxUFSZZphH9ILk7STrAcAbiKy3edEQryUbpiSxiqiEP3aKQgjW+LOAMDiLHP5p2GmDOoNWHMAFDuTdeigL3QV7KVfDcMG5W+WzzMO5UEecy1DlMUeyzyKN5fINSP+CdsTIQIiCi7mQHWgFikIxaZjMmo6eyaVY6sdvVWWj87BcgZWMumAYTNgUJvoQUbRYNZr6Klr8bsVna+i+hMn7RvBbSKufsTgpzloCG5CxAGA2FRvRkIlNkxMAIBOCMhdhQOkPyLpYN8rqQewrWDoiu9mXmqh6Ui3u7Q3FjoNXvACX0G3U5Hds5ALggG4gBE+g1F3CIAd4w9yOfRdJwCDPkM5GLLQw2tX7YCzVW4bcmBBrFoKLSrNiHlZcawBxVXdszyPIVZZ+PAdAgYXL20IfYoHpxshIRmFr6C9Y3iF1Oehbuh/FEu9EWFB6DEA3oQXkEJ3P0vup4ICPEY5Qu9H2CdNFuldyIwLsRS0ldBJ1OsUkB/ALURuGrkS6suBdoAxTlY7JKekHw3eTLgmuvwoSYEB5/EBYgJJhYDQ3M7u7D0rC6rgT2xJRnQcJ+4X3DQKH+AyX91OrVsTVnmyKZy4giu5VOCo8JdPRA5XQqmiLdQkXYcFwtYxQvGLFZ42wdTXbyIXYAQZPkyGEr1hbMURnJeua/dOoxApogaUD019jFIwmagsQ9qGcnc/IRQecz6gFT3TvxSrUAJlcbVzx9iuWEdyupaewXslHheUAT1EXpLDk2SiczIC1WhuyRoANXotkbeebJmlxhUTaBmV3eIvaa0cFKEIgiRGIWSMxMMVZijavqoxX9QfAQXuKmV0kba5b1gPmXdtdKE+v4eRi4N41hpFA3GrZuCK/esSkR++Dwh+O12GI53WvRd4vadQB6ArAncoZ1MkuydmiOpe9XQNwAXSfcl4V3kp17qS7/feVZ7XI2oq9LsxOpK+elepMnACbkFbXS0XbbfwsLGpQgaMiDuIMfdDQ0tHtE5JpJiYrDfYQoWAzsBH1VSUsfAKeU+ef5cDlaRU7qtyF1jhox1Ks4ODt0VLytBHeSh5lk9Sugj+0XccowBFziap4WXkrk843xUJAEH8Ai1vJOU2yjvMWimdzPI713AH0m4OSnqMFa73czk/gEd93sAvEINyAF2gcmXRBuugB7RCyT5wQc10DkJgvIK3YuAH0YUPQdPSeseh6HQyeMUWzIDAl5fJYKXb7n7yh2BFBf2AXmkp6sXsWLLA4/ZNe9syAGdsBMIH3Smzoezx9FMXSsfEr94PPwBB8ACQTYFzix8BEODvKYedl4XmMFOAbQC1xoRMxqyACMnP8pLVYkUYQh9nQ64pV6oGTJvhBEheKoAnQ0/BPlBggfuPJWC/sDUblzsIo0ial6FvEIK4AOcXr5FYG9LMNr8SfwWFl7jpDRdUIX+qyLVFqgbZnI+LhdpMovSZiG7wkAEAR1IGShlbR4byteFkgL7F1zngFDks/pOh+DD5GTn0BRuVN2Ew4FUDMT+LYkPcQXpbA9cCtRem5XPEw2LyS3JIp3SsDoy/QZhBFHxTkk6UI8BfyRTpZlEhEgmbZqwJybt+IuTut4FrF8YCoXqH+JrkITTikWbXngRK3rBQgzN0FAUyPnVHiBiYgIOki7i6SjF1yWx8C9QD3LvFOCmjVCmj8LUP8jttqhKCP6KyC0163ad4C883zK6ejJdBUuCZH0twCJkm5GjjXJB4uC2jQY+/ZNyiY4tf1MBUN8Zo+l4CI5CzfBSH4HHUdI0ICw7SBzL3Vs9bBQvDZahfWNJ+EOGRg3yvQJAESTcoXkZQjZ4ApEu3+myX8qnXa3K4pX7uq2ZGcghSn7ux4DCQNk7FJC4Dw+7Vgjv0UT6V057IJPgLBVGpGKGubY6KaYGQrQeSRxMTeQDk4rtIs/hAKCKwv0phpAhRq0njMIxFgTYnYG8fCHXnkr65KBHmkCrcRS/ZEu9VgepfXoZJDUuU9UH/AU5iYgDXi6NbG+kaR74XQ8rlQ8AUcQdPCN5xnbqtmuSwLSNgWlQGOs3DypHoDjuSzAATJNyulTkg6Ya0YF84QEz6tX7leBrSfwharmiexpD5mx/sMCadDRs42VJLG6fb1Thi8BAaPtFGg9R0QkoD4RAAC0k2K3rHRNjEJ2JJmQfeIYwOy8AclSZfUT3S3Q7utHBWNXKnsPKKkYnSkDha3xBwAG7c7DnJrT5kaoBcsXyESvM0Bu+2muV6MzvBECDAhYONPfCUSxBYfKBRh9IK9kHgtNQDyK7kg2d1qvEcFkhJN0LZud7K72XcLmsS5K9p2GRK8n0TK+jDbhR+MCMhOjrl2HpT+5++jmznOeBCEo97Suzzqg55bO6vtWKZfKh4EPiL1VAV9VB2PL1PfyA511JHTON6AuQRpAXnLcAJkm5ERndkF910IoE1UYvemdfgOjL+ZeVwkH2ufZPgIzuGTjNyhwAGWQVARXAgWtyJ2/Bd8lpbOGwhShGRbCLuEwqFRWZ0ydYTtAe0GBEeamGqAYD/wDoCQ43J3eG9RPJQp4opgY+AsUc8ZxLyaL1B/gTvBAVwys/a7i2XBBX3UNVAOIwgDAbVBqDVQNS5QAr1yPK+8LwulI6Jdfz/doyiorAc2IdlkrQolBus5PhcFYPhcdI7CF19G9O6/mtiFAKo6zHYIlMFJ6D9ZBPoOsLd5cUb3f8ALWue6V2YuS/gHiuEnfdykcCjWv3BYGBFIgiF5x/QFUI5x2oUUpxGI580vINz/VjgmAtuoJ4EhVP2K1ApklHFnkUfwCm7xsmPAJuIRcAIDio6nkm3V7NivgwesSkSa6VmS61JiiWn5QoYRXcSwENEyFQIpmfYjyPwXtduVD5mJoap82xzAconI31JI2DNWKgFpD0mUAwzKR4NfDzNZuGdnJ01Q2yaEMWgFynTOS4ao9B58STaTij8i1qPRnCAtox5zWZKcD6iQwo4gKs8vIGmDdSu7IVwCug9DB3lwsu5DuSiX9U9K16DHBQW9TIHtN3+gertCxBq/bOXBfJDTQm4rsZGC8bR5U+JfgF3N5J6jo1t2jgFwG8OFvVSoxDjsBSNUb4AYtcQoZODQ8izzFxDFgw8UNT5kNS/AaXKI6P/QfsrV4kK0lo31Ai7zm2AtKf7AMoHce1S3I99hw9ZHZuQER6rjlclMMSTouNcPhFO05LvkcSguGvCjMmWFm+SsTjwkcoVQPiC2Ld2afIRKyEoOyZ/Cfs1fAYcj/EC6+04QpwTPQtVV5tsRjyHvJCJ4WlkJC4BRelShLbSB5SzkRbHfcxLXHwigTT3kQX8y+IFwRCMUOr9LSt6U3w0K/IF6lz0vEyDlBpVEQT9Iu4kvMF0Lfg7ohBGL7AF/E6ahyhgb+mLBdZD8A/K7ERgJb5AGrgKzwXJg5YrtOu0rvIMQuK18Auj0jAiNAbORzjYvFA/sw7Hb5mlqTfEagEtLQMBQWpr5gMOTWMSAAzQZN0kq6dpkELsrWH7JtNtWzzLkOyFqfZPpF7sLKgU4BBoxy5rD6syGOB8L/CG+FPbMFNdFQ3hZ6QUMgJ7Zmu6rk1TCwwUQ/sgz5qqU7UGEvrgOyROjHNuH4CbgTiDLs0+gKYkAFw+Ig7VIZJGImQF6FqVO4wKDYnZdoF5BcUF2IVQjDLaLazCH6lgQAXBOG5Xa10hdZMRrhC74VYnyhXfGlPhX9L8bwsagMTc1UNQlx0Ivuw9Kb2835XAv8AgLgik8LwvxySuGM8Quwv4rvvtd5DrSEATlgtiG7IrVIOI7fUPfIBqo8wwgIcqMK2cYmh5wrxeNwEUGsWFgSAFyb8IFc8zz3UHjA6P0VQog/gijyK+nG0rseyHfKKYCQAqrj8OfsmwWp1M2Mff8VRAbAzsVnu1j8jVCdLucpTYRMro2iwohB+yzrpWZsinTtxoCxxy70mNA+Eb4Y5MgU1niLSgKNGhmyAnfmwvDhWVvWYwpk4XCjTHCckxHNRaOnggeyd5go+xxEcAt2TQJx/sFxCfyjYYlPWanRYeMbzgIrrq/2AWf2jTytou7V1UaOKVOKAJI8CuaXD93wrImV8LhWgASp+9RGu0sEbbQfkjm57w6IQfsyRrtDaSvEv+Au+wxK6sfJeMU8UBqxqH7B20USV4aVxRDEi6iOA96nPAUsgAZCA4TJqeJw5F4RTmQgLxMlYad6tpaUtQFh0IAcrr8c10Li4LvbMV1rm511nnJEoD3mUEOALPGzKXOfwGBPNR1BIiMhE8K6BephwKCDoTnL+GqoQxDkA6b5XpSYHmhlFAwAAkABADCqB5LwwAtJuE0Wu4zv9fCQrR9Geiu+Jai6iuK18gulrwC153iyuY79hdmFT6F3fpU9VL1AK91vQKKAOY6ll6Tgih+yiTRuSb64LF0Z1pbKXCwX6FdEUnk2ChdjYGKF6AQoWGIRsrymowekZooRSRnJJmSaJ+AP1RUXskuRSRa2k7nJGmlax7dZW0fhCgHXGIEgDjBdSKuFKDJqhvkMssmUQQUxhNtjh6wmYDFMOBQwv56GCsXdYY7LJaTcFk4yO4SazV0G1bB17ZY2qFUC00dheSi4p85uZOEKoGHiqJIjSxFxoRQCKFmf6gWk3BNsEkW5PIfEhX0DkdP8AKMmTKEgHTOLipE/wALojBrDgYVHqG+AEfMNHZyHNEgsSDVpgPfw2dzQfpChh9pnRXFBdngWJZnV8i3IclP6fa0ZmvvOgqINZf1kACgNV2cPR9M19EBTGRDYIxd48IbSfnGGTZwBXkWCswNjOJbmhvhf4TKIFwEKApiYd1tZi2dlchRQQAKo3mGSZ5fADHAwxY8DlbV1e0W2L7lP+v4BTZ20yuiH1UKByxWSEqI8hfDkfjAnJRtE2AmSbkSmInuBK/b4BCQZ6Q6gvR3PRuIkQhXPVHqn2nKTbFijM/gEc5uEBk/dGk8p7FOgVsA+GMzOvJwnDge6iV+QwoRDrHc6BYcmVJhsAsxUycqSAg24mtjUl/StzFBY80OaP0K6obhbDkrYkkeTwwrtFqBvh9BzZNT8gqNkR0iVByqSNBxY1pgjtQjlgphiMyXRwhETMzJe5tgfkahTITeYByik4p2ZnR7FAjxWBjh2xXg/AZHbk4hxAKKOaxin31QX30uHJQrvQI8QvkAO1ocEdGJAPdXlf8T9zErixCv8AF9wzAW11R2Jda00vBof4g9JPCLkDKnPxhyEDqIkjMcyoIwdaoJzMGQJ0N5+EmLq/kWn6ZQqcZXAgDA5hd7a+65rKg5oGoGe4H2mQV7J9kYzXL9E7UtOg1XSJowjq+I1TfEv16V+Y1h1jZwF5Jkpj2vnxFbuUdwXTubnXe6Mlxl/2XAFeAK0q7xKPeUZvonOEQ8qgKShYaBfRQstvc3qqWpoacglHhEU8eFDLIyhAcCrzpWZBqWUwVjiM/wACRDVi/hq7p6buoT3kbHP5JMWo/ndBdiDc6J97rClDTfBCkD4/mCuht3suf+PsuJr5As+IH3Xge4SP0fgAbilgId6S1Wg/gMwe/jl3LXGfqnWn72RMaGoZqBlhOHse43HBMwI2SrAXXOF3NmByY41vU6dP8BQNVrHSAy8ixgI8vWvkJunlh8IOlG/SytAe6oFB7bYDg1U683Dt/AFAE0oVAqiggnO5/AeoojEpY7HIBh4rHunoGg64sqJAhA1F+Ytg/v4wK4dOni1OcmO8QcGFzGsBkThQPQPlgIlx9YjsRpGRhG1GcAfIBgpfEVxgtJNLIPFHvUbA+jV3J5pRITtq/ScUfJsu0VvBhM1/4au9B66NJ4v51zljA6L+PgJyAuxAAzKgKhvI6JwHAoIABekuCt+rWh4QUBTgOOXpnpFCMck8PwH6xFEFXA/AHBnseWJhRBPUXk9J2GSwi7RQLd/sXgze6s6k/hVx6AYeYZ8ArqSeS9S/tOtR4lH+SzsnFd2UdR9e4Q9WLAUcE1BvjIyls5Az0WqCQQUU6kZycInL/Hbih+fvfVB48XllThDmsHalXgtOTYAL+IbwgOkrqZBOSz5fGCL0FSFBq/yZp9qosTX7RU63fxRP8Fv4NY5fXJLe2iJckr0x2CaHYBgGqym7mDcijJOpRTBsMyrRCdD8iU9QmZgDIL9IhvAD4QihlinIOu9SYKVHqsJ1PzwMCcQnXFzAx5NYNtMpxuwCkUobEGduOdA6eoALdzYYnMJjUXIHtVm0xuF6NSKUz37UEapjchcA6rxvwUC8V4AA4FWccrIh9IhT5kk58/PdRcK6vYA1poQCdmyj8wJBmOAZrQJaNfseqNOpugiwXSbm4cCxfSFkCweaqUChtfhPIQzSIVavQTAjJEGrOrR9CP0rsnKRIWlXyAsUybiuuzoOnpA9cO+RO1D+Dw6L+Ky7+gh/NHvkS6wA65djeXRia/I46AJmls+kESdS3NGZuQu9iXCg/cpwOWUYwlzJdAOQFhC7yad6Z+nQh4US0RtueAVDlEbJmeIO8DanqI6db7kh4Fsc6np70FAORyAaodW4yvcSAOjpyg3nOxQtu08kLtfF7rpEsE6pfBl5wvMVKnreQpAJhAaCFI6eh4iPkvyN6AVEPw66xUh3COqlzN8TagGXMc44BOnJvhMT8rgGSGZDMbrKPQMjYd05QW/AYt9qTQWTXByVx3+BYAsAuoA/GsMEgC9Q22vgA5tMbQ2DoVTqLUHzPRMmwEm4HXEUW7Kx/Yw7rz87wAtqfzDozmNx0Yvs4xUiKHoej9YAzRf3WN1LnWgAt0HqTSPT9pCY+qGvaQi4FYDvHgQOTx8C3kfSA5NG9QIK40m4FFigTqc8BaQGQgOE9B6Gmx1lzcoxzGdERJAW0GVuCESUA4BBiCDcRSek+YsqJAhCnFOQtg/un3yGIDk1Y6eg3yCK0/gPPp4H4w9yajvvCX9ClQeC5FJD5uL4psNIAHAed58LVbV71Zh8DKGKaqicz/JqrgZfQwqHauWjjrIygHgj/cFMy7E1cPhB06Zb2VRZn4ZqacK9mBEeTUdfrABEq0Oo1MOBR2BLwCBT1H79ijyTSyakJ10I74hbU8IKHoKd5I+Iw4FH8CTD2hVHDggxeGqNf3bXsP4nrDetNYehFo0Rb8bOCMGjQLsuUT/g/qOLJesY2KJRf5BuTdgAGZXOMIp0Ta4o6oxUwb3WD53dDcG4oqtXmULA1lDL6U9AINwif4LnqU9FnqAJCmjUSimUfvWiewdMHlywGCf4qdHpdPS9FgNMB3cC3KZPQ52Si/sOwsrAkSC20eUS5JUE47UspR0cgA9IFqgv3ewOueP4hRSAiOQdc2S9LwzPMu9cwddankFd82DMRJbroO8ItgAoeg9DwKOnymV0JDL+6x5tJHDgVBdww4gkQnNMeNsHDga4iwcc2c4mNRcgILWheVpSI5Daofg96Et0NRwV8+Xf0BdUb41Zzf3tE9hahQCEUdNYT/gXXjcFPAG0UeQT3nVUwR89zmkyhNOk1LLrdHt5fhiXXP8AMEwV1zjBmTzO1qi9gSBtJpk4nl9gOaB06ehb+m2eaIvGgUOVcZI1sxwdgpsWIFyuliXCsFVKW2L/AKBHZOERADkBfs0EPmqK8NSw9nqxvJbMszsDFY658LwcQYHGvbyA14eWaB0cZoESIWkFKt/ETFAPiOnTwIA3kmij0RAakLFbb1F0cVNIAxCTSPlJcTA4Ae9jD9UCzKI2hT3wAuWDb8B03APMCiGOBBOh4hqDL6U2AJXCJ/iD5xDUgmKxNwcNSDSYe7lgZPVPQ/wHZoTEuosxamsOSOd0n1bkqFOW+gIDaoC9bJJwFY6bgjFQzaR8x9fhgQsuHAEyVPudvXniaM6xkieAvqZVXpGG5T7WZkv7rSi3UWWQyOhpDjlXkriBSdJpgu8lkEVcl6giEUWvkbhBtCgdlOA4FD/K0foHULRtg3BHhdCPRcgf7IXeHYp22GRXbvYFZ/QGGnUbcrvdZhFodMsXqn8EefsxND8Fl43IuqeQZLC1BMvXEF2p/DjiFokP2pQIesTftU2VP4gmMvwddXes0LBDZceATZBsEIBBPXHov4E0kl1Dyy/ZIBAsDDmx/K6XJa5D2/lOBY9Kc81+XdEm9qSevQNVllpPIBz4UQbRxMEEZZWiYEEPwAdyc9gdsmU8heRRbcuCk6Ex4xUxO0pXlblWeCgaP2mMDSryVxoMBDYarlHHtft5ObynP4gE3zFvmgQSw524QBqvYcCQIT4op0oMViwaD8MRQAFhCOLRINFhNjuCRUYbnHD48kJK56uyMCrMsdftIu4IVR1h1ju5KzppnxDlZGf2gV5RnM9LpKcZMakLy+E1MWQPDDk8VgWs4Q+HRiaBo1s88BQqN8Ynw0GBqYLTZuBxP0+a/iy2bTiY1Gz4Y9CDCdiKds3BeIwXMdPdBIibfgH/ACBQRy1AkMbeqoOaYezLbKDP+EERJkKwNMVPRMQ9PxSYmmxGDQkFUDTkvwIVwdmcFyftP+B93Ag88E1Xx2UEFIBi6fhI9JrEJUdxCZEQT1XHKeRXUBYFdUngFz5fsroM9V072C54/tW8V7crV7sjak3+INUG+CMsDXYCkWMbAcPZUAY0EHZxopDdE7m85+IEcJECCrvcPB1oPzrLMrabnMjTo1hxj4aFS6bVAEFPvjwBrXGghWJC5FPSZlPP8CwpUph3TrDzyDETyarK7Y7SSNS/ZIv4v3XSoYrrHcBQFG6nD7UbzWGlXUCMbA/CPhdaEuoRqeWz5LvZ5p4ohOsvehquufC7n6RnCd7qMh2idaKCdsHJPtp8FeeC8ChpR0JK8LqN5TdB/hcVqjFCq4DmzqCbaSH7zoWEQBEL8rHrZ+ORMAxi3RFPIA3lFbmeK1Hv8B8CnBhqDdrtJz8DSuegiZb2P+DZmL/PcLG259siHeXFdExgyJQvM8o3AMI8BX/UUDUjVAHWa6QUYAuPxXX81K2TX/DFILt5WU7E5e9BS5Fum7WQjdfyK77nJdLOuC47V4FCOwq86BqwTE/uFdh1zSr0utYaIXWEZIT2NtIm/Q6ZFAajqLV4LSBdfa/XHCgF0IeMrsSksrAb8GRLCAoKOQaRFXU/AfamMEI1Q26/QnEBHJvKH46W2zHq1MgBGDc8j3+bOPVUF0j3zka6LyxtJ5f8kGq8qgVwZdxGTOTwtuL1ALqV0cuViFZRsYIhAH2U5QgmzwopAXSah6UhQqHQw7yrGyxxsCvK5ORdqZkgPRVwu5AxKKEY7DBdkWgQXuDeF0BWJRSAYD5FBIrX1WfS7xDJWNLDv1PS/sV/bL+xQ7D4Q6r4XWhZLzwXpeYI9LvCsF08ciuwPkmOBxPEouey2CFioyI70e/TT571B7VeQmNqSP4YepUJ0Y/GAWO1iAxFb5idqgNOWdwFAuMcaeU349gRSwPNeFCURbhtCZQLhlkkdnbp/c/na6dNv8p/JYVzzIacByUebWOZn+IZMmpH9iRcACy1mTdKzQu1fpXQIEAChREtzAJWDYgGw+CAACX4n7Uo81xnGzkcsfGd2XcXYlNujYEF3sOWCAsA73cVJNZM4K0U6GK7eTearAD8o3+EAF7bRunQCcjPQARSxIo48C2PZH8kKGM7XU2QLEb5jmPwXz7Ohx5hpX+uDjrG+MaodCOy/fMZQwhnS8fYh+BL862qgDFg5koEOAZoKYPRgcLGxfyKozurgMM/BkO6KImvBgPAreGONyKB1ZfgOpsHAbAgL2xeZXqAHwFuyZNQN+QGQTVAP8HFIt5NBb0iCGqhkCV1BPNn+x9p5DJlpP5ZXBMRVj1PdTKACMiTyFV+ZN8JxZAC8mSs0AYziWpJrWkaXvyXPwmrmTL9idkCCy2fnreh2KNrSIarImRy5V0glY2oHX7iPBF/Pd1ACP3IBkeuG8sJKuIxxowXcCcy5VsRYHcALnVHcrqB8UG8PxoVoZMmpDVx+QPUAoQ+eqP8uz9eYgqAW8rIJmYU4W/M/m3NuXLQoPwzFaKBLnYWisIyT0oaFgYH5pWgRaPqHghWQph7wC2Bkp+QMyX+cZ1DNU/ZF/75tLusmIyLQDBygNHLbMEMiiGrMds7qypjY6mCCGRiPKVkUvur6U8zbeNCMnI5fnuUPy3gy0AZkC8OtkyaoDf4w0iEo6IAaw6V3czTABAL2tx7RLx/OI/YA+RQqBC9RrJ2Q5KhaRQ+FRA+sGTy6mKJu0eqrGJWNtn2OkkS/wARkyZPOmxI4CwrxH08yPQt3nhI2KlR38eQBkEM4gzkrHTcq6BuV+qgJQ6Ev7DQ+lEsP5tCMmJnJuU6b4H82fwBfyrgkKbPIN2LyvTZy0JSYBtMPJXPWTOGQPAXe60oL97vReaM8hctBfETHwV1r/BDNbjhhWZqZR1rDXHxAuXWqAyauG/LACJRACb8RldGwUIAhXxjcbyy8ouI1yMyT+eQEXrwt6pASjNvqsD8DsYQaMyZQ3jxgsOSuR5haJ1NK77vtBaTgFlvX0Fs1OziwYk/In01zIARWetrjBi4RZu/5Z2pWfMIekFmZKR7WoiAuy75XcDg5+S7QPTvjB+LCcDzOgRWw4nNFX5u36LBQx7+CPtWSvfSg4Pb8SBzjNz5R0gEtdir3Tr+R4qE3JX8I0QI4GSQu7Mc4Qq0CvsTK7xm9IY0EUBZZrm2A1SJPx1ADfkkBoEFQ2BFlsXZlDnrQj0TqaYmSj+jBsgEE/5zolw5wVDfbG1jqPFEgJuZIQU/iAcPHiPHx02oEvb00B3jpSgMwOY4m1gN1cRrwhIfEEyBzrb82QGZVlAH70QQfLMc5RwGjLeUg9cUW/oKB5Jww/ehn5ALuchMBmUFbHP2W6NWnkcyMOSuoRx2Cn36JRX7yHlesAOVuPhsRXGWx1YFsGO2DrY0T+l0P2IrZPVwnjxvFHKRzzxrLxFNODnzYGRNBwohmcZ+YDMQ9LGCY8voWqDC0vuUUbRTe03RqS8NBdj0Wn4XcMZAFhXtvBXYZXIAcmhV8m7wiLZYILpgQNwFgctc4vwGUBvgSAvSh8se5eu15KvJi1vKA2Gror7cKT/wBBO6FpODR5zUbYA7kUUanLMVhGIVhfg7MKoMaqTWiOGyOXKer9Evcgti2xzZSzob4j7EOukWAcq5ieTzMWQIkME9aoBOMSjnuReRFhoKo3wAfvpm7czIarucJ/YuWPzAoNlZVMwDuDI55Jw8rEBhAGpTUbtpuIcrIBYPKfC1hpuUrVgqNh8FYBkb4j6S+G32LxkDahJGEAFITCC4BBgQRaCINasbuv8A5rVkkantRMU175KEAyS2NyWvHyIfBT4btHPA6FOBKQAbhOICYmWYM1/EV2PAsqx22fIppQy+SenxTgNcb1g+DBNUHALUmElQbk90uUZGCCIui+84p2/wpKyEJ0BgcDfYU5G6nirxC3IulxwKfv7ruPwXt/VHAhgiIWoTBtXCtl60f0MqoyZNW/RhlcjWNXH4vnCNXg6yKxc4/YF2OwktFZpiB5pB6sD9aFtlpIy6AsC8V39L9/jkyvGK4FZtAwWUnyJ2qJqtTwZnfyDWgLePLCTGdr2Fih6DxHfiQIvq6zRzrENhd6B5N+Cxxh870IcSf0EngK9R7AXZZ0Dlky137HhZ4DcrnZb9IDKsoDVXrhk1AphpCuD/AO9D4zNMAB3fZPCKUJckzJ/xXYuLE6kEjgWMA3GSHQChkhGCHmJ4t3TARN6+W9MgBqAyZNQb5zVB5jdKOpZisNOwk2HKk7GUBGQo6zMtKkCzDhWldaAXahsrDuyYWEIPCdHoBQ3wEjOqC2w6akWiW1alMb2aIx+JIIwJLGHoyOBWznKQNkeGoR/gsZk+AwzYou5TCgCxLEGiVNXXe9qTAYEvTsBV6TBmZh2AoiEljSkAGDSU7CCqXledF5HGazn0cCbgDs1cln74lIMQ/wCKgkopvgP2rHCmvYUICAKZDxp/QL0dIn+OBIiuQmCmcozL1zEd2BpiRJQLgi3Jz8/7IAo0NQalqGoNQNS+ilGZogs/Mgm2OxXiuTIILKr2zoRjgs2ZkhifrdZxEVqY1WCImQROsAJoJmFXeaDhYYpYM4B5MtICYHKg4SAoplHwxObkLxajKWtAXG4giIIgR8JAxo2LAMR6NzU7PJ8NiGnyChVqhcma+8x8CeBTFjdStQCQrEE7R7Fs0WS8gDM1mKiDNqflwYqDPdhLEEK7COq55ZMriW8OAsDCOCOQVzAg3EWfEgN8JGoBqp80HLDH9IdAKMjB4wCIyfL3Q/ySljrLcDeFlv8AEvvo2iEwjmR6AMBSHF5HynBH/AAEyZ0VeSA8r+Znt5DstuThYI+oFxac8jbOgda06+9qhgtpXxIqVGb1frTPKGCZS6x/SxEPFCVBGBgx8pNlkws3We3g2giINoomruFKu1E2BEEYgsV6Q8rcY5GhYR7dY6bNHWBu4iMaz1+xxnzRfrAARPCjXOxwlt5icqtEWB+ZDlmGOK1ZEeQrtxUoJCIAZsvDYsZ4rI6zl/oLEXVMGTfEG+M85fzVj+oQrysKADoB3KLXBFkLzif8suixY3RVjxEx7SwciItpXL7HBAV14+VaysPSN8QA5J2OCTgEZwfYCzhPJeyStCmTiYplrSBG1kEcF3jyG3VtST8mAIs20nmh9g3GBReYLoTcHSkWv8yYETjImNRbTvWMuIW1jv4UDqQ+9i9m1YN9D8Z80JiDDeutWV8I/wDoGbmTYpbvybtDEQUmyvdxtELxFYIz84izuRFt9Qj8DfJZBqjYVIFHVggQ/ZZEpAL79ymf80k8ESDtIi4iwqK0Ru8ULCLRZkQaRh+1TTgYWZfJmOPjX4aqKDKuB3yBlM3KTl8G+ZM30NiHc1IHMsVpGSGB4Da/hAjL+KRrRgn2RCatNfh5g4guNE9FjvxAxRMmMC9kit+WXDEOy6yCu5ZXlO4SXJ2msgKeAY0aydrWYcjRKPhL2zwQWhO5dYxivW3LiF2X2BSZe+gIXdclspyi9uJ7RdQfucZBcgzJEevgxc3cnOVgQrnBoyybBDAJgvgwg3ETBCioFCF32SXbCNvaZhJkRBpBqjfEJRNU/n0+b7PCFPVhQQbzuwFvdERacLgsv843mYGKZLiOZFdmussRYcGNM9HnPuDAwsqmr/gTDBl9dJiTZszUiiM49QRvMUEijmAAEyTcr+ZC+yYmGat754X6wlVwhrV71TGvHPYBYWIxsT8K/I/pggGnlwATypyBqgziThIQrgEsmRpTVgNVCLzi4mE2C8xkoc6kTcFjjrnIT13J2jZE1ScDA4TU5SVmoBBYaTciQPsYpKYJaMBskCWEQNUBqB8m1BZ6DRt4PsgWVYWCAQL7uYMylTFdy/0Z1qFRLyD9GCAxY3BsC8exMHQ0iL+qkQPVU0PxJChqBxcu6rsH+rltAMESASAWuLI91pnIXqaUQ3C2YSwtqKxq6vVLaG02Au0Gpvoh9iTrkpMO2lsfRYNY47AUkUL1SgPQI8ruA15XecZrzjHhdV9L7J/SczYJEB4BrqIEucg4uZRJZcsq8GtGg0gEjmY+0Qy/pIxL7AF00XPEuFgQGzggdDWMWoKbcIci9WYyfHEcsxarUijhHlt2MVeDbVisw73xpBk3ymJaOMn9MjhGGKYMtkgEIE5XUjpHVyRL/wClG92AxB4D5kVL048E8iLZhiGNKiVrC5Q8TyU/kEAgiynT8Pxqgf8AOHsRIALvgZMMSyy+hIB0LUJjwicFpBTBcSAwHyNxWppPtAJ/S3uliQu8daZuVl5i3kBzqmxrA6tNQd0CGu027AcqVsFBYBDi9jSRc/zsCAQFy7nXNc0v5Jhc2qgR4oDDBq5Q5zvwIcUM3W2U8EaoiURTubFxXaeYlWY614l3i4kzhQVWjaScpCcaECjUEnx1EJmDBbg5LEobJqLKZj6HVhJFaGYsqdLvKVk7ZIrJjXEJ4t+zC/YfqEG2QjFimgAbFTWg7g0LJk1INPwqlmaHJOM3DymYQMjJMh11ckmZlIzG8z9f6kUXYoC62g2BegEcg3NpDoiHCn0KAmUWFm3HFHnvBhBp81KNWGQL5XoRYFAkd+A8t9qWaa+2hfvDsFl6IWBdEJJtJtPx+o00edJm2q6gGWBc6nRYPp+9p61HwWFCmcldogDlk60mD7UaZxnIV3GJIGiMnV6T0pjoyICf4ABHWAcyAbhjQMh/RIQ4NKdOiAhpINxEiuBjEI7wOtF+P7nOY3w1R/KYfg5LGSB+APK7jWWaDoStFCwUaDA4YdqOBdCe+V/RgiY1iwXEGIpps7huBIgiRC0cGT4w22XLHQMXrEG0WjFlzOcpq0G+mFUCo+7IFottQGLFhRwBA4g8UXPs7eU/+tJM/YsbiIhPI5wvxsk2GWAwTpKJmRBk/IPC4w9C8tFezmqAyHfa9CJALEiUg3Um2SGGUlmAAmSTIBfQWwBbyn4h1IKsfwe1lbr3Jcl1wNilo1SDxToYP4sA1IXaJq05TeXNezNXsYctynrF29GmOhIFvVI9YftAKR06tWCxTeKxImV2GbUQoXdWuNuYMVMBReGoMaJTWRC5jrBOTDYhdwDzT0JpLFL76sYFaaifu6hxlSsEMgDAQRIjFbIJp62i2d6tvE3R8ngxvRNxZ0Cyg3wC+L2urFAOs3NjTEDUEaibgL0a+EDycf8AYkrHEhO1ELQUBsgxr2ZfxW30NRoiSRlyPeXQVGY/EF5hTk1LNTTlnmhJMgBetY5rvMtOiGDcnmAJkm5ZEFhMfwDf4VMKtbtmZAZoGPm+6dOsL0CxPIWaCCEngAfxxAakL9jwg2G7rqDFBCh/hC9EmrqQf30zPwrdHocOQKTLIL4DY8gVmFlFuCIO6sD+Wt56oIZmh9vIwOdV2WwqyMPYWaMSsJNwM1CgDHAIwIgaBuRcWouF5HK1gxivKRoLmUN4CRBvT0cFl19hcOnn+C8Qu+EL3FH/ALo34L+LOTAYChIFIINxZdvR9LWCR/tE6o5S6nOnjcrKASQNA4rxYCFbfHxFzkJz4Etd5RbtYrLhFDDKTzABMk3IoB8iQ/gFQr1Eq6b53FCAZjnOtg3QO6s2fEmZOJpjqfrshPQ6AdMr9Bd02YDyQTp/il0+0ApIzHIBhwEEl6b9TyZCb4UhYmZA+lJOXoGg/Su4qA06MQP38MjQvX1KIMVFuB5t5B4Keh09LM+x/wBMwhW7QdTbl+gAi8G5Ts7j5BmDiFiUNue0xiNaM7+tewZNcv2bKAwMwVpSVG8eBr91pNj9AYqyjL5HFBtFQBewsszovdImxgP90YKBEsFGZg6xuS29WUMX4v8AsQNMFJtQ9x2IU+ufmxGIrW3LSjq/kvNAALti69bBainRRT0TUXb0GBSzK1fRY9pTK6ZHABHdlpzzpSJfX26s/wAMXxnhyKCdN/uAngNap6Dp6Zgut11HUCyMDwV6AkE1c0Z9CVtwOWNCTblk07EImNb/AIbxonbkyyUnts4eAYjaiBZaD0DQou54OJcQot4GBtz+7CojSYtM7iF1i1cRJBRRbeO+EuhRqTcVGAFJD6xErEJF5KSYk/78lBi8ZBuD7jhOcDM9t4g4o50hHSA2XY2B9KKgbI0WuKnv519RiZIN5sqBIJtDPoA3XnRGqMoTO4szooZoUvGzMacLtEbC9iwXNtTpYp8dXg613qPWWDE2vJGk7MBG5cUD/AF9Xgjc0t8TA+l9feu/5+wxWJuRSl2+is8M2ehmMMvhG3rBxMCrQzIAj0sJ+M5mF4ImrBFiPKOQrvsbCLhtBvVvcHDHoVCu71sMuU4PuziJhBZofdOV5dkZq4YCnIBgNZdaaXD+qJJMf+CdxLczni/GYTrNaR7U5i5WEEkcFCSvQREBzWs+szWsJKwzDwae03H+/EyX7rjmgkFZmY2l5GxRVANRGL4P+JjNSETFg+zeTQb/AO3kaFvPSeSXkp0NXhr+EzDRMSykWpxaZ1LnVOmFhRwAdGn5AS0l8IOnVuobrjyaQHaLvP6PgvplkexMY0UOXJtL0yKJ/M3UO7fEfXJSxbXlWsUWXAsPQsPbpvsG5de2RaCoea7CcmSyelIlAm30j3fQUumomDSkiDupF5/mKLmyKAJqJ/4KKIQFyWIOBTyH/CCRwahW0AgDteGgGIQQkQhbD06npYyYdJmcDjJQbBbgeQC1p836EqiDKDXNnJ7egoNiHD8khQH2MBeTIBYrx0O4VtXSxkJAfiS73sXB9aVEcO/sWbH5MePQEeaX9W058BNyidAkVJflX0TbhFZm2V2yz+GXxiZreBQW7up9jFTghtPqNoU7o2mDNsSnU+wkFAIpcYONRMHhf4Ublpjk/wDDWq2aOUQ+AzWxRsC/R3hC9k4BJRAdaGLGxUVZwtSZa0bwtc8GckajKNNkr8eplSx/gIeChE1r0XB7MF1Xw9s/FfOUX4pK0Ubmi3hogtiBFYAEyokmwrpG89fkfyUQW6G8aP1NxyXn4W4wvfzE0AguXEDgjMLqI4FXgvztbhkKjVg/Ce7I4XWGzV93wlcwVgGQQhM0GTJYijkFvhD6wBFmKn/xUjkJ2IXS5txbxEMFJn2AhBeCAZKNRRgm8xG6x1pxHSRUy+bQrghzALyuU3tjyC0+KHmCuatYFO3j4hcMB+MFzs4z6RzpwZGLPTT+E9QFgTgv6S4UMmTmQPxL3VaAmfYaorC/yPSyWHApzNt3IoFYPy66vh0XR54Sg0YzMyZUCiiGQY6fZXgfH78InrYk/wCMEl5UTppLG8pliE0zfptg8r6cw4iY1RMo6XZMlziN3Ot63mv24BsGS/c1VF1jM1VBvwm72576Vh68b5Tw1DGS+VADk7KK5mUJfeZ+Ym2f3AoD+zOaIccPUPhMdZsokVBX0cnoZjaygXJzYGWbwyTJqj+lBEJ0WQEjgwsaqSv94KGkkKOkAVJgRLSxmiXMNHM2oo/8cShVFEEFiDmhI8yYcBzjiuJuPJ4I1JqPrQc9FA7EINqPbrbUJSZHZ4Gh/G9Ak1Z57XLJ0KNIXB3yKOQx+f8AoQsfdDT3dD2v4PZfFB7NhXZwfYWv4P8AV1S1Rn3HOwl4anMsszgnRboSEMLGqxU2K/WlKAWBmQsTJ+wskBGd4S8kUK5knJT/APIRQTqTDFsyIcX/AKTs9iI22p+vgVGokQ6BIwEyXKCK2pR0SbrQPU+Rtriob5P0NvmZkFZBhJhQAGQoLVbhfScSV/OecMAIfP0EfpoewIELDbnjQKw1IkP/ANPVxlQoSdtzyPrJRhzNj6Le5MTGGoxqajANAc+4UPgRWazaQmzbEZkG0/VgoZA/8rHSZh9wGBRqbU5np9COgFJ36jyW5SFIgauLOnUANeF3m3YUqaaMmoZNWPlKy8TJWhfE+qTOmDFFnhHIJDX8DqTMUqL/ALyAJqpqtWLONV41EE+wP20zXVMt7SyU0izmNQIUYcT+eYbIcb7oh64JtoGDlhAf8yysovW8+GYSOqAASmMXUrKRYBBmcQVAiusaaC7CrXksyh8oV1cZMmqBvNS82mQxp7rANy/UfgBYqHb4yggNQuaGs8rqAhBdIvQkWDQQAKcAA7q0AG6HidIcCNuYobQxOrRaZkMgCApx/wCdmM1QuEM9fJbkCG9kd3IXS2qBqYGLOgi4V9K2z0XYFbcs2oj2IfCK70I+5mSsA4FmJCiZ7J15uDEleuoCwMAK5qjVTLHL4CidZm4npIppmFLPMl1ggsFpyu2BAhizhxRwMUAYAxO5BFAWEoBfddjS+4jaaRwKP/RZoCkSEOsK4dD4CgROsvoFQWRllr2jqXNK9UYGgh/SAusjrFtRd3W6NPYrVL4coIGjeCVl4ru4roJUgLe192QSFYrJqRqRqox7topHs2FAzLxpuDPhNGOj3ldIHYSWFQgBxU4CASNWMBQ6S2HeuUWxcfYAc6NURm484cW6mRJpw/6Y0JXBpDZHQ1j2swUewS2dLwmXsEyNxOQVT4sh6KaEQHWHAJ/C9Cw8xXOYn9KVQDe7dStauxyveFvKsHUei5/CX/8AzCWdyBDLY6RyXXnrF9JnGWAGH2u+DxVxQRHhX8UoCNEBiNoad8QqEgQCLW9tbHR30EXDNEMCxev4n/6ydEoQRu4E39ajwO6aXE2BzQZupzI1p2UzSR+a2agaaAaiFOOmyyXNQ+BXksnyKA6ctF6K4w3oVtaZ5C8Jz2r2db1juuK7i8rtBzQEPWbZeQL0KxnMeaH6eGB8BSbpFqdTB2lXSObioeKgXQVkgiLDGg4PROp6AnuC7N/BfwAsRHITU0//AGQEYE7q96Z8gEwTX6oPgggDy7iAeEcXZ3+DV3y14XKI9lsGA00oFDj5YEZESWPGBQgg9ANIKMiXDSvRY7ld7vMKstwHI9KcGv8AQYDhROI7A2GIu5JJ/wC4nbS6BWFGHLm9CGbs20I/BCz7wrkyEOQFYTVQh7eWwLJm+tK9hq80UeqKs4Gn7X0F+14xK/SlDn6MX11VkWn7UkWlfugueVX1h8ig+k0W5pU+nX3RLf5XbkhXJGFOTQVr/wCBOnN6fed1iw4iF/SJjIMDyXWPSFyOhG9Qtbuftf1n2sduVityrS2UNNd9kYQkZlOb0/8A4e6io0unTp03/tX/2Q==");
  background-size:cover; background-position:center center; background-attachment:fixed;
  color:#f5f7fa;
}
[data-testid="stAppViewContainer"]{background:transparent !important}
[data-testid="stMain"]{background:transparent !important}
[data-testid="stHeader"] {background:rgba(5,12,20,.20)}
[data-testid="stSidebar"] {background:#080808; border-right:1px solid #3b2818}
[data-testid="stSidebar"] * {color:#f4f4f4 !important}
.stApp h1,.stApp h2,.stApp h3,.stApp h4 {color:#ffffff !important}
.stApp p,.stApp label,.stApp [data-testid="stCaptionContainer"] {color:#dddddd}
div[data-testid="stMetric"] {background:#111111;padding:16px;border-radius:15px;border:1px solid #30343a;box-shadow:0 5px 18px rgba(0,0,0,.22)}
div[data-testid="stMetric"] label {color:#d5d5d5 !important}
div[data-testid="stMetric"] [data-testid="stMetricValue"] {color:#ffffff !important}
div[data-testid="stMetric"] [data-testid="stMetricDelta"] {color:#ff9a1f !important;background:#3a1d08;border-radius:999px;padding:3px 8px}
.stApp input,.stApp textarea,[data-baseweb="select"] > div {background:#111111 !important;color:#ffffff !important;border-color:#5a3518 !important}
.stApp [data-baseweb="select"] * {color:#ffffff !important}
.stTabs [data-baseweb="tab-list"] {gap:8px;border-bottom:1px solid #4a2a12}
.stTabs [data-baseweb="tab"] {background:#151515;border-radius:9px 9px 0 0;color:#eeeeee}
.stTabs [aria-selected="true"] {background:#3a1d08 !important;color:#ff8a00 !important}
[data-testid="stDataFrame"] {background:#151515}
.stAlert {background:#151515;border:1px solid #5a3518;color:#ffffff}
button[kind="primary"] {background:#ff8a00 !important;color:#160b02 !important;border:0}
hr {border-color:#4a2a12}

/* EURO PICKS visual identity */
.block-container {max-width: 1440px; padding-top: 1.1rem; padding-bottom: 3rem;}
 .ep-topbar {display:grid;grid-template-columns:1fr 1.5fr 1fr;align-items:center;gap:12px;
background:linear-gradient(110deg,#080808,#1b1008 55%,#080808);border:1px solid #4a2a12;
border-radius:20px;padding:17px 24px;margin:0 0 22px;box-shadow:0 12px 35px #0006}
.ep-league {font-size:13px;line-height:1.25;font-weight:900;letter-spacing:.8px;color:#ffffff;border-right:1px solid #5a3518}
.ep-brand{text-align:center;font-size:clamp(25px,4vw,43px);font-weight:1000;font-style:italic;letter-spacing:-1.8px;color:#fff;line-height:1}
.ep-brand span{color:#ff8a00}
.ep-brand small{display:block;font-size:9px;letter-spacing:3px;color:#c8c8c8;font-style:normal;margin-top:7px}
.ep-live{justify-self:end;border:1px solid #7a3c0b;border-radius:30px;padding:9px 15px;color:#ffffff;font-size:12px;font-weight:800}
.ep-live i{display:inline-block;width:9px;height:9px;background:#ff8a00;border-radius:50%;margin-right:8px;box-shadow:0 0 12px #ff8a00}
 .ep-hero{display:grid;grid-template-columns:1.25fr .9fr;gap:20px;align-items:center;
background:#111111;border:1px solid #30343a;border-radius:24px;padding:28px 30px;margin:10px 0 18px;min-height:190px;box-shadow:0 18px 45px #00000055}
.ep-player-kicker{font-size:11px;letter-spacing:2px;color:#ffad55;font-weight:800}
.ep-player-name{font-size:clamp(30px,5vw,54px);line-height:.98;font-weight:1000;font-style:italic;letter-spacing:-1.5px;color:#fff;margin:10px 0}
.ep-player-meta{font-size:15px;color:#dddddd}
.ep-hero-portrait{width:112px;height:112px;object-fit:cover;border-radius:18px;border:2px solid #ff8a00;box-shadow:0 0 22px #ff8a0033}
.ep-hero-portrait-fallback{width:112px;height:112px;display:flex;align-items:center;justify-content:center;border-radius:18px;background:#292018;border:2px solid #75400f;font-size:46px}
.ep-player-directory{display:grid;grid-template-columns:repeat(auto-fill,minmax(145px,1fr));gap:10px}
.ep-player-card{display:flex;align-items:center;gap:10px;min-width:0;background:#151515;border:1px solid #5a3518;border-radius:12px;padding:8px;color:#ffffff;font-size:12px;font-weight:700}
.ep-player-card img,.ep-avatar-fallback{width:42px;height:42px;min-width:42px;border-radius:50%;object-fit:cover;background:#292018;border:1px solid #75400f}
.ep-avatar-fallback{display:flex;align-items:center;justify-content:center;font-size:21px}
.ep-player-card span{overflow-wrap:anywhere}
.ep-match{text-align:center;border-left:1px solid #5a3518;padding:8px 0 8px 20px}
.ep-scoreline{display:flex;align-items:center;justify-content:center;gap:16px}
.ep-score{width:70px;height:70px;border:2px solid #a85a12;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:25px;font-weight:900;color:white}
.ep-vs{color:#dddddd;font-weight:900}
.ep-teams{display:flex;justify-content:center;gap:20px;margin-top:10px;font-size:12px;font-weight:900;color:#ffffff}
.ep-date{margin-top:12px;color:#dddddd;font-size:13px}
.ep-stat-grid{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px;margin:0 0 18px}
 .ep-stat{background:#111111;border:1px solid #30343a;border-radius:15px;padding:13px 8px;text-align:center}
.ep-stat-label{font-size:12px;color:#dddddd;font-weight:700}
.ep-stat-value{font-size:27px;color:#fff;font-weight:950;margin-top:2px}
.ep-stat.primary{border:1.5px solid #ff8a00;box-shadow:0 0 15px #ff8a0022}
 .ep-panel{background:#111111;border:1px solid #30343a;border-radius:17px;padding:17px 19px}
.ep-panel-title{font-size:15px;font-weight:900;color:#ffffff;letter-spacing:.3px;margin-bottom:12px}
.ep-panel-title span{color:#ff8a00;margin-right:8px}
.ep-insight{padding:9px 0;border-bottom:1px solid #4a2a12;color:#c8d5e8;font-size:13px}
.ep-insight:last-child{border-bottom:0}
.ep-pick{display:grid;grid-template-columns:1fr auto;align-items:center;gap:20px;
background:linear-gradient(110deg,#241306,#120b06 65%,#0a0806);border:1px solid #8a4a12;
border-radius:20px;padding:22px 28px;margin:22px 0}
.ep-pick-label{color:#e8c6a5;font-size:11px;letter-spacing:2px;font-weight:900}
.ep-pick-main{font-size:clamp(26px,4vw,42px);font-weight:1000;font-style:italic;color:#fff;line-height:1.1}
.ep-pick-side{text-align:center;border-left:1px solid #75400f;padding-left:24px}
.ep-pick-pct{font-size:42px;font-weight:1000;color:#ff8a00;line-height:1}
.ep-muted{color:#c8c8c8;font-size:12px}
@media(max-width:700px){.ep-topbar{grid-template-columns:1fr;text-align:center}.ep-league{border:0}.ep-live{justify-self:center}.ep-hero{grid-template-columns:1fr;padding:20px}.ep-match{border-left:0;border-top:1px solid #5a3518;padding:18px 0 0}.ep-stat-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.ep-pick{grid-template-columns:1fr}.ep-pick-side{border-left:0;border-top:1px solid #75400f;padding:15px 0 0}}

</style>
""", unsafe_allow_html=True)

st.markdown("""
<div class="ep-topbar">
  <div class="ep-league">TURKISH AIRLINES<br>EUROLEAGUE</div>
  <div class="ep-brand"><div>EURO <span>PICKS</span></div><small>ANALYTICS &amp; BETTING INSIGHTS</small></div>
  <div class="ep-live"><i></i> LIVE DATA</div>
</div>
""", unsafe_allow_html=True)

YEARS = list(range(2026, 2006, -1))
BASE = "https://675hoops.com/downloads/player_box-{}.csv.gz"
TEAM_BASE = "https://675hoops.com/downloads/team_games-{}.csv.gz"

def belgrade_day_key():
    """Cache key changes at midnight in Serbia, including daylight-saving changes."""
    return datetime.now(ZoneInfo("Europe/Belgrade")).date().isoformat()


@st.cache_data(show_spinner=False, max_entries=2)
def load_seasons(years, refresh_day):
    frames, team_frames, errors = [], [], []
    for year in years:
        try:
            r = requests.get(BASE.format(year), timeout=25)
            r.raise_for_status()
            df = pd.read_csv(BytesIO(r.content), compression="gzip")
            if not df.empty:
                frames.append(df)
        except Exception as e:
            errors.append(f"Player box {year}: {e}")
        try:
            tr = requests.get(TEAM_BASE.format(year), timeout=25)
            tr.raise_for_status()
            tdf = pd.read_csv(BytesIO(tr.content), compression="gzip")
            if not tdf.empty:
                team_frames.append(tdf)
        except Exception as e:
            errors.append(f"Team games {year}: {e}")
    if not frames:
        raise RuntimeError("Nijedna sezona igrača nije mogla da se preuzme. Proveri internet konekciju.")
    data = pd.concat(frames, ignore_index=True)
    team_data = pd.concat(team_frames, ignore_index=True) if team_frames else pd.DataFrame()
    return data, team_data, errors

@st.cache_data(show_spinner=False, max_entries=2)
def load_data(refresh_day):
    # A new cache entry is created on the first app request after local midnight.
    return load_seasons(YEARS, refresh_day)

def safe_num(series):
    return pd.to_numeric(series, errors="coerce").fillna(0)

def avg(df, col):
    return float(safe_num(df[col]).mean()) if len(df) and col in df else 0.0

def fmt(x):
    return f"{x:.1f}"




def normalize_player_name(value):
    """Normalize names so the 675 Hoops list can match official EuroLeague profiles."""
    value = unicodedata.normalize("NFKD", str(value or ""))
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]", "", value.lower())


@st.cache_data(ttl=21600, show_spinner=False)
def load_euroleague_player_images(season_code):
    """Fetch official EuroLeague player portraits from the v3 stats feed.

    Do not cache empty/error responses: a temporary upstream failure must not
    leave every player with a placeholder for 24 hours.
    """
    url = "https://api-live.euroleague.net/v3/competitions/E/statistics/players/traditional"
    params = {
        "SeasonMode": "Single",
        "SeasonCode": str(season_code),
        "statisticMode": "PerGame",
        "limit": 500,
        "offset": 0,
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; EURO-PICKS/1.0)",
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://www.euroleaguebasketball.net",
        "Referer": "https://www.euroleaguebasketball.net/",
    }
    response = requests.get(url, params=params, headers=headers, timeout=(8, 25))
    response.raise_for_status()
    payload = response.json()

    # The feed's envelope has changed between releases. Walk the full JSON tree
    # rather than assuming the records always sit at payload['data'].
    records = []
    def walk(node):
        if isinstance(node, dict):
            records.append(node)
            for value in node.values():
                if isinstance(value, (dict, list)):
                    walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
    walk(payload)

    image_map = {}
    for row in records:
        name = (row.get("player_name") or row.get("playerName") or
                row.get("person_name") or row.get("personName") or row.get("name"))
        image_url = (row.get("player_image_url") or row.get("playerImageUrl") or
                     row.get("person_image_url") or row.get("personImageUrl") or
                     row.get("image_url") or row.get("imageUrl"))
        if not name or not image_url:
            continue
        image_url = str(image_url).strip()
        if image_url.startswith("//"):
            image_url = "https:" + image_url
        elif image_url.startswith("http://"):
            image_url = "https://" + image_url[7:]
        elif image_url.startswith("/"):
            image_url = "https://api-live.euroleague.net" + image_url
        if image_url.startswith("https://"):
            # Store common name variants as aliases. Historical box-score feeds
            # sometimes abbreviate first names or reverse first/last name order.
            raw_name = str(name).strip()
            tokens = raw_name.split()
            aliases = {normalize_player_name(raw_name)}
            if len(tokens) >= 2:
                aliases.add(normalize_player_name(" ".join(reversed(tokens))))
                aliases.add(normalize_player_name(" ".join(tokens[-1:] + tokens[:-1])))
                aliases.add(normalize_player_name(tokens[0][0] + " " + " ".join(tokens[1:])))
            for alias in aliases:
                if alias:
                    image_map.setdefault(alias, image_url)

    # Raising prevents Streamlit from caching an empty result after an upstream
    # hiccup or a season-code mismatch.
    if not image_map:
        raise RuntimeError(
            f"EuroLeague API je odgovorio, ali nije vratio player_image_url za {season_code}. "
            f"HTTP {response.status_code}; odgovor: {str(payload)[:350]}"
        )
    return image_map


def selected_season_code(season_label):
    match = re.search(r"(20\d{2})", str(season_label or ""))
    year = match.group(1) if match else str(datetime.now(ZoneInfo("Europe/Belgrade")).year)
    return f"E{year}"


def fetch_db_player_offers(player_name):
    """Fetch current offers stored in Supabase for the selected global player."""
    try:
        matches = (sb.table("players").select("id,full_name")
                   .ilike("full_name", player_name).limit(5).execute().data or [])
        ids = [row["id"] for row in matches if row.get("id")]
        if not ids:
            return pd.DataFrame(), "Igrač još nije povezan sa globalnim katalogom igrača."
        offers = (sb.table("player_prop_offers")
                  .select("market,line,bookmaker,over_odds,under_odds,captured_at,game_id,source")
                  .in_("player_id", ids).eq("is_current", True)
                  .order("captured_at", desc=True).limit(100).execute().data or [])
        return pd.DataFrame(offers), None
    except Exception as exc:
        return pd.DataFrame(), f"Ponude iz baze trenutno nisu dostupne: {exc}"


@st.cache_data(show_spinner=False, max_entries=2)
def load_upcoming_database_games(refresh_day):
    """Read upcoming fixtures once per Belgrade calendar day."""
    now_iso = datetime.now(timezone.utc).isoformat()
    games = (sb.table("games").select(
        "id,start_time,status,home_score,away_score,home_team_id,away_team_id,competition_season_id"
    ).gte("start_time", now_iso).order("start_time").limit(50).execute().data or [])
    team_ids = list({x for g in games for x in [g.get("home_team_id"), g.get("away_team_id")] if x})
    teams = sb.table("teams").select("id,name").in_("id", team_ids).execute().data or [] if team_ids else []
    team_map = {t["id"]: t["name"] for t in teams}
    return games, team_map


def show_upcoming_database_games():
    """Show upcoming fixtures refreshed on the first request after midnight."""
    st.markdown("### 🗓️ Predstojeće utakmice")
    try:
        games, team_map = load_upcoming_database_games(belgrade_day_key())
        if not games:
            st.info("Još nema predstojećih utakmica u globalnoj bazi. Raspored će se prikazati kada se poveže i pokrene izvor za to takmičenje.")
            return
        rows = []
        belgrade_tz = ZoneInfo("Europe/Belgrade")
        for g in games:
            raw_start = g.get("start_time")
            try:
                parsed_start = datetime.fromisoformat(str(raw_start).replace("Z", "+00:00"))
                if parsed_start.tzinfo is None:
                    parsed_start = parsed_start.replace(tzinfo=timezone.utc)
                display_start = parsed_start.astimezone(belgrade_tz).strftime("%d. %m. %Y. %H:%M")
            except (TypeError, ValueError):
                display_start = raw_start or "—"
            rows.append({
                "Početak": display_start,
                "Domaćin": team_map.get(g.get("home_team_id"), g.get("home_team_id")),
                "Gost": team_map.get(g.get("away_team_id"), g.get("away_team_id")),
                "Status": g.get("status") or "scheduled",
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    except Exception as exc:
        st.warning(f"Raspored iz globalne baze nije dostupan: {exc}")


# Poziv funkcije ide tek nakon njene definicije.
show_upcoming_database_games()

refresh_col1, refresh_col2 = st.columns([3, 1])
with refresh_col1:
    st.empty()
with refresh_col2:
    if st.button("🔄 Osveži podatke sada", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

COMPETITIONS = ["EuroLeague", "EuroCup", "ABA liga", "NBA", "WNBA", "NCAA", "Ostale evropske lige"]
with st.sidebar:
    st.header("🏆 Takmičenje")
    chosen_competition = st.selectbox("Izaberi ligu", COMPETITIONS, index=0)

if chosen_competition != "EuroLeague":
    st.title("🏀 EURO PICKS")
    st.subheader(chosen_competition)
    st.info(
        f"Izabrano takmičenje: {chosen_competition}. Ovaj izvor još nije povezan sa bazom rezultata. "
        "Ne prikazujemo podatke druge lige kao da pripadaju ovom takmičenju. "
        "Kada povežemo verifikovan izvor box score podataka, analitika će se automatski pojaviti ovde."
    )
    st.markdown("**Planirana podrška:** raspored, igrači, box score, forma, protivnici i istorijska analiza granica.")
    st.stop()

with st.spinner("Proveravam najnovije objavljene box score podatke..."):
    try:
        all_data, team_data, load_errors = load_data(belgrade_day_key())
    except Exception as e:
        st.error(str(e))
        st.stop()

required = ["season", "season_label", "date", "team", "opponent", "venue",
            "player_id", "player", "sec", "pts", "fg2m", "fg2a", "fg3m",
            "fg3a", "ftm", "fta", "oreb", "dreb", "ast", "stl", "tov",
            "blk", "pir"]
missing = [c for c in required if c not in all_data.columns]
if missing:
    st.error("Podaci nemaju očekivane kolone: " + ", ".join(missing))
    st.stop()

for c in ["season", "sec", "pts", "fg2m", "fg2a", "fg3m", "fg3a", "ftm",
          "fta", "oreb", "dreb", "ast", "stl", "tov", "blk", "pir"]:
    all_data[c] = safe_num(all_data[c])
all_data["date"] = pd.to_datetime(all_data["date"], errors="coerce")
all_data["min"] = all_data["sec"] / 60
all_data["reb"] = all_data["oreb"] + all_data["dreb"]
all_data["fgm"] = all_data["fg2m"] + all_data["fg3m"]
all_data["fga"] = all_data["fg2a"] + all_data["fg3a"]
all_data["3pm"] = all_data["fg3m"]
all_data["3pa"] = all_data["fg3a"]
all_data["season_label"] = all_data["season_label"].fillna(all_data["season"].astype(int).astype(str))
all_data = all_data.dropna(subset=["date", "player"])
all_data = all_data.sort_values("date", ascending=False)

if not team_data.empty:
    team_data["date"] = pd.to_datetime(team_data["date"], errors="coerce")
    team_numeric = ["season", "poss", "pts", "fg2m", "fg2a", "fg3m", "fg3a",
                    "ftm", "fta", "oreb", "dreb", "ast", "stl", "tov", "blk",
                    "blka", "pf", "fd", "pir"]
    for c in team_numeric:
        if c in team_data.columns:
            team_data[c] = safe_num(team_data[c])
    team_data = team_data.dropna(subset=["date", "team", "opponent"])



with st.sidebar:
    st.header("🔎 Filteri")
    seasons = sorted(all_data["season_label"].dropna().unique(), reverse=True)
    chosen_season = st.selectbox("Sezona", ["Sve sezone"] + seasons, index=1 if seasons else 0)
    season_df = all_data if chosen_season == "Sve sezone" else all_data[all_data["season_label"] == chosen_season]

    teams = sorted(season_df["team"].dropna().unique())
    chosen_team = st.selectbox("Tim", ["Svi timovi"] + teams)
    team_df = season_df if chosen_team == "Svi timovi" else season_df[season_df["team"] == chosen_team]

    players = sorted(team_df["player"].dropna().unique())
    if not players:
        st.warning("Nema igrača za izabrane filtere.")
        st.stop()
    chosen_player = st.selectbox("Igrač", players)
    venue_filter = st.selectbox("Domaćin/gost", ["Sve", "home", "away", "neutral"])

# Official portraits are retrieved separately from SportAPI and cached for 24 hours.
image_season = chosen_season if chosen_season != "Sve sezone" else (seasons[0] if seasons else None)
player_image_map = {}
image_source_season = selected_season_code(image_season) if image_season else None
image_error_message = None
if image_source_season:
    # CSV season labels can represent either the ending calendar year or the
    # season start year. Try the selected code first, then the adjacent season.
    year_match = re.search(r"(20\d{2})", image_source_season)
    candidate_codes = [image_source_season]
    if year_match:
        y = int(year_match.group(1))
        candidate_codes.extend([f"E{y-1}", f"E{y+1}"])
    tried_codes = []
    for candidate in dict.fromkeys(candidate_codes):
        tried_codes.append(candidate)
        try:
            player_image_map = load_euroleague_player_images(candidate)
            image_source_season = candidate
            break
        except Exception as image_error:
            image_error_message = str(image_error)


# Fotografije igrača ostaju u profilu izabranog igrača (ep-hero).

player_df = team_df[team_df["player"] == chosen_player].copy()
if venue_filter != "Sve":
    player_df = player_df[player_df["venue"].str.lower() == venue_filter]
player_df = player_df.sort_values("date", ascending=False)

if player_df.empty:
    st.warning("Nema utakmica za ovu kombinaciju filtera.")
    st.stop()

latest = player_df.iloc[0]

# Season/filtered averages and recent form
last5 = player_df.head(5)
last10 = player_df.head(10)
season_stats = player_df
player_name_html = html.escape(str(chosen_player))
player_portrait = player_image_map.get(normalize_player_name(chosen_player))
player_portrait_html = (
    f'<img class="ep-hero-portrait" src="{html.escape(player_portrait, quote=True)}" alt="{player_name_html}">'
    if player_portrait else '<div class="ep-hero-portrait-fallback">👤</div>'
)
team_html = html.escape(str(latest["team"]))
opp_html = html.escape(str(latest["opponent"]))
date_text = latest["date"].strftime("%d.%m.%Y") if pd.notna(latest["date"]) else "Datum nije dostupan"

# Resolve the actual final score from the team-game feed. Each game normally
# has one row per team; never invent a score when the feed has no matching rows.
score_left = score_right = "N/A"
if not team_data.empty and {"date", "team", "opponent", "pts"}.issubset(team_data.columns) and pd.notna(latest["date"]):
    game_date = pd.Timestamp(latest["date"]).normalize()
    same_day = team_data[team_data["date"].dt.normalize() == game_date].copy()
    team_key = str(latest["team"]).strip().casefold()
    opp_key = str(latest["opponent"]).strip().casefold()
    same_day["_team_key"] = same_day["team"].astype(str).str.strip().str.casefold()
    same_day["_opp_key"] = same_day["opponent"].astype(str).str.strip().str.casefold()
    own_rows = same_day[(same_day["_team_key"] == team_key) & (same_day["_opp_key"] == opp_key)]
    opp_rows = same_day[(same_day["_team_key"] == opp_key) & (same_day["_opp_key"] == team_key)]
    if not own_rows.empty and not opp_rows.empty:
        own_score = pd.to_numeric(own_rows.iloc[0]["pts"], errors="coerce")
        opponent_score = pd.to_numeric(opp_rows.iloc[0]["pts"], errors="coerce")
        if pd.notna(own_score) and pd.notna(opponent_score):
            score_left, score_right = str(int(own_score)), str(int(opponent_score))
score_left_html = html.escape(score_left)
score_right_html = html.escape(score_right)
st.markdown(f"""
<div class="ep-hero">
  <div style="display:flex;align-items:center;gap:20px">{player_portrait_html}<div><div class="ep-player-kicker">PLAYER PERFORMANCE PROFILE</div>
  <div class="ep-player-name">{player_name_html}</div>
  <div class="ep-player-meta">{team_html} • {latest.get('position','EUROLEAGUE PLAYER') if pd.notna(latest.get('position','EUROLEAGUE PLAYER')) else 'EUROLEAGUE PLAYER'}</div></div></div>
  <div class="ep-match"><div class="ep-scoreline"><div class="ep-score">{score_left_html}</div><div class="ep-vs">VS</div><div class="ep-score">{score_right_html}</div></div>
  <div class="ep-teams"><span>{team_html}</span><span>{opp_html}</span></div>
  <div class="ep-date">◷ Poslednji evidentirani nastup: {date_text}</div>
  {'' if score_left != 'N/A' else '<div class="ep-date">Rezultat nije dostupan u izvoru podataka</div>'}</div>
</div>
""", unsafe_allow_html=True)
stat_values = [
    ("PPG", fmt(avg(season_stats, "pts")), True),
    ("RPG", fmt(avg(season_stats, "reb")), False),
    ("APG", fmt(avg(season_stats, "ast")), False),
    ("UTAKMICE", str(len(player_df)), False),
    ("3PM", fmt(avg(season_stats, "3pm")), False),
]
stats_html = "".join(f'<div class="ep-stat {"primary" if primary else ""}"><div class="ep-stat-label">{label}</div><div class="ep-stat-value">{value}</div></div>' for label,value,primary in stat_values)
st.markdown(f'<div class="ep-stat-grid">{stats_html}</div>', unsafe_allow_html=True)

st.markdown("### 📊 Forma igrača")
a, b, c, d = st.columns(4)
a.metric("Utakmice", len(player_df))
b.metric("Minuta", fmt(avg(player_df, "min")))
c.metric("FG%", fmt(100 * safe_num(player_df["fgm"]).sum() / max(1, safe_num(player_df["fga"]).sum())))
d.metric("3P%", fmt(100 * safe_num(player_df["3pm"]).sum() / max(1, safe_num(player_df["3pa"]).sum())))

# Učinak igrača po protivniku — SportAPI boxscore za svaku utakmicu.
st.markdown("### 🏀 Učinak igrača po protivniku")
st.caption("Minutaža, slobodna bacanja, šutevi za 2 i šutevi za 3.")
try:
    # Profilni CSV i SportAPI mogu zapisivati ime sa različitim dijakriticima.
    # Prvo tražimo tačno ime, a zatim proširujemo pretragu po prezimenu.
    db_players = (sb.table("players").select("id,full_name")
                  .ilike("full_name", str(chosen_player)).limit(100).execute().data or [])
    if not db_players:
        surname = str(chosen_player).strip().split()[-1]
        db_players = (sb.table("players").select("id,full_name")
                      .ilike("full_name", f"%{surname}%").limit(500).execute().data or [])

    normalized_name = normalize_player_name(chosen_player)
    exact_players = [
        p for p in db_players
        if normalize_player_name(p.get("full_name")) == normalized_name
    ]
    # Keep the exact normalized match whenever possible; only fall back to
    # candidate rows if the provider uses a materially different name form.
    selected_db_players = exact_players or db_players
    player_ids = [p["id"] for p in selected_db_players if p.get("id")]

    box_rows = []
    for offset in range(0, len(player_ids), 100):
        ids = player_ids[offset:offset + 100]
        if ids:
            result = (sb.table("player_boxscores")
                      .select("game_id,player_id,team_id,minutes,ftm,fta,fgm,fga,tpm,tpa,dnp_reason,source")
                      .in_("player_id", ids)
                      .eq("source", "sportapi")
                      .limit(10000).execute())
            box_rows.extend(result.data or [])

    # One SportAPI appearance row per player/game; exclude DNP entries.
    appearances = {}
    for row in box_rows:
        if row.get("game_id") and row.get("team_id") and not row.get("dnp_reason"):
            appearances[row["game_id"]] = row

    game_ids = list(appearances)
    game_rows = []
    for offset in range(0, len(game_ids), 100):
        ids = game_ids[offset:offset + 100]
        if ids:
            result = (sb.table("games")
                      .select("id,start_time,home_team_id,away_team_id")
                      .in_("id", ids).limit(1000).execute())
            game_rows.extend(result.data or [])

    team_ids = list({
        tid for game in game_rows
        for tid in (game.get("home_team_id"), game.get("away_team_id")) if tid
    })
    team_rows = []
    for offset in range(0, len(team_ids), 100):
        ids = team_ids[offset:offset + 100]
        if ids:
            result = sb.table("teams").select("id,name").in_("id", ids).limit(1000).execute()
            team_rows.extend(result.data or [])

    games_by_id = {g["id"]: g for g in game_rows}
    team_names = {t["id"]: t.get("name") or "Nepoznat tim" for t in team_rows}
    output = []

    for game_id, row in appearances.items():
        game = games_by_id.get(game_id)
        if not game:
            continue

        own_team = row.get("team_id")
        if own_team == game.get("home_team_id"):
            opponent_id = game.get("away_team_id")
        elif own_team == game.get("away_team_id"):
            opponent_id = game.get("home_team_id")
        else:
            continue

        fgm = _ep_absence_number(row.get("fgm"))
        fga = _ep_absence_number(row.get("fga"))
        tpm = _ep_absence_number(row.get("tpm"))
        tpa = _ep_absence_number(row.get("tpa"))
        ftm = _ep_absence_number(row.get("ftm"))
        fta = _ep_absence_number(row.get("fta"))

        minutes = row.get("minutes")
        if isinstance(minutes, (int, float)):
            minutes_display = f"{minutes:g}"
        else:
            minutes_display = str(minutes) if minutes not in (None, "") else "—"

        output.append({
            "Datum": pd.to_datetime(game.get("start_time"), errors="coerce"),
            "Protivnik": team_names.get(opponent_id, "Nepoznat tim"),
            "Minutaža": minutes_display,
            "Slobodna bacanja": f"{int(ftm)}/{int(fta)}" if ftm is not None and fta is not None else "—",
            "Šutevi za 2": (
                f"{int(fgm - tpm)}/{int(fga - tpa)}"
                if fgm is not None and fga is not None and tpm is not None and tpa is not None else "—"
            ),
            "Trojke": f"{int(tpm)}/{int(tpa)}" if tpm is not None and tpa is not None else "—",
        })

    per_game_df = pd.DataFrame(output)
    if per_game_df.empty:
        st.info(
            "Nema SportAPI boxscore zapisa za ovog igrača. Proveri da li je refresh "
            "učitao njegove utakmice i da li se ime igrača poklapa sa SportAPI bazom."
        )
    else:
        per_game_df = per_game_df.sort_values("Datum", ascending=False)
        opponent_options = sorted(per_game_df["Protivnik"].dropna().unique())
        selected_opponent = st.selectbox(
            "Protivnik", ["Svi protivnici"] + opponent_options,
            key="sportapi_player_opponent_filter"
        )
        if selected_opponent != "Svi protivnici":
            per_game_df = per_game_df[per_game_df["Protivnik"] == selected_opponent]
        per_game_df["Datum"] = per_game_df["Datum"].map(format_serbian_date)
        st.dataframe(
            per_game_df[[
                "Datum", "Protivnik", "Minutaža",
                "Slobodna bacanja", "Šutevi za 2", "Trojke"
            ]],
            use_container_width=True,
            hide_index=True
        )
except Exception as per_game_error:
    st.info(f"Učinak po utakmici trenutno nije dostupan: {per_game_error}")

tabs = st.tabs(["Pregled", "Poslednje utakmice", "Protivnici", "Odbrana protivnika", "Analiza uticaja odsustva saigrača"])

with tabs[0]:
    chart_stat = st.selectbox("Statistika za grafikon", ["Poeni", "Skokovi", "Asistencije", "Trojke", "PIR"], key="chart_stat")
    stat_map = {"Poeni":"pts", "Skokovi":"reb", "Asistencije":"ast", "Trojke":"3pm", "PIR":"pir"}
    chart_df = player_df.sort_values("date").copy()
    chart_recent = chart_df.tail(10).copy()
    chart_recent["date_label"] = chart_recent["date"].dt.strftime("%d.%m")
    metric_col = stat_map[chart_stat]
    chart_recent["bar_color"] = chart_recent[metric_col].apply(lambda v: "#ff8a00" if v >= chart_recent[metric_col].mean() else "#777777")
    fig = px.bar(chart_recent, x="date_label", y=metric_col,
                 title=f"{chosen_player} – {chart_stat.upper()} • POSLEDNJIH 10 UTAKMICA",
                 hover_data=["opponent", "team", "min"], color="bar_color",
                 color_discrete_map={"#ff8a00":"#ff8a00", "#777777":"#777777"})
    fig.update_traces(marker_line_width=0, text=chart_recent[metric_col], textposition="outside")
    threshold = float(chart_recent[metric_col].mean()) if len(chart_recent) else 0
    fig.add_hline(y=threshold, line_color="#f3f3f3", line_width=1.5,
                  annotation_text=f"Prosek {threshold:.1f}", annotation_position="top left")
    fig.update_layout(template="plotly_dark", paper_bgcolor="#101114", plot_bgcolor="#101114",
                      font_color="#eaf1ff", showlegend=False, margin=dict(l=10,r=10,t=55,b=10),
                      xaxis_title="Datum", yaxis_title=chart_stat)
    st.plotly_chart(fig, use_container_width=True)

    st.markdown("#### Prosek po lokaciji")
    venue_table = player_df.groupby("venue").agg(
        utakmice=("player", "count"), PTS=("pts", "mean"), REB=("reb", "mean"),
        AST=("ast", "mean"), **{"3PM": ("3pm", "mean"), "PIR": ("pir", "mean")}
    ).round(2)
    st.dataframe(venue_table, use_container_width=True)

with tabs[1]:
    show = player_df.head(20).copy()
    show["Datum"] = show["date"].map(format_serbian_date)
    show["Min"] = show["min"].round(1)
    st.dataframe(show[["Datum","team","opponent","venue","Min","pts","reb","ast","3pm","3pa","stl","tov","blk","pir"]]
                 .rename(columns={"team":"Tim","opponent":"Protivnik","venue":"Lokacija","pts":"PTS",
                                  "reb":"REB","ast":"AST","3pm":"3PM","3pa":"3PA","stl":"STL",
                                  "tov":"TOV","blk":"BLK","pir":"PIR"}),
                 use_container_width=True, hide_index=True)

    st.markdown("#### Detaljan učinak po utakmici")
    detail_options = list(range(len(player_df)))
    def game_detail_label(i):
        row = player_df.iloc[i]
        date_label = format_serbian_date(row["date"])
        return f"{date_label} · {row['team']} – {row['opponent']}"
    selected_detail_idx = st.selectbox(
        "Izaberi utakmicu", detail_options,
        format_func=game_detail_label, key="player_game_detail_select"
    )
    detail = player_df.iloc[selected_detail_idx]

    def shooting_text(made, attempted):
        made_value = safe_num(pd.Series([made])).iloc[0]
        attempted_value = safe_num(pd.Series([attempted])).iloc[0]
        if pd.isna(made_value) or pd.isna(attempted_value):
            return "—"
        pct = (made_value / attempted_value * 100) if attempted_value else 0
        return f"{int(made_value)}/{int(attempted_value)} ({pct:.0f}%)"

    st.markdown(f"**{int(detail['pts']) if pd.notna(detail['pts']) else '—'} poena** · {detail['team']} protiv {detail['opponent']} · {format_serbian_date(detail['date'])}")
    shot_cols = st.columns(4)
    shot_stats = [
        ("Šutevi za 2", shooting_text(detail["fg2m"], detail["fg2a"])),
        ("Trojke", shooting_text(detail["fg3m"], detail["fg3a"])),
        ("Slobodna bacanja", shooting_text(detail["ftm"], detail["fta"])),
        ("Šut iz igre", shooting_text(detail["fgm"], detail["fga"])),
    ]
    for col, (label, value) in zip(shot_cols, shot_stats):
        col.metric(label, value)

    other_cols = st.columns(5)
    other_stats = [
        ("Minuti", f"{detail['min']:.1f}" if pd.notna(detail["min"]) else "—"),
        ("Skokovi", f"{int(detail['reb'])}" if pd.notna(detail["reb"]) else "—"),
        ("Ofanzivni", f"{int(detail['oreb'])}" if pd.notna(detail["oreb"]) else "—"),
        ("Defanzivni", f"{int(detail['dreb'])}" if pd.notna(detail["dreb"]) else "—"),
        ("Asistencije", f"{int(detail['ast'])}" if pd.notna(detail["ast"]) else "—"),
    ]
    for col, (label, value) in zip(other_cols, other_stats):
        col.metric(label, value)
    extra_cols = st.columns(4)
    extra_stats = [
        ("Ukradene", detail["stl"]), ("Blokade", detail["blk"]),
        ("Izgubljene", detail["tov"]), ("PIR", detail["pir"]),
    ]
    for col, (label, value) in zip(extra_cols, extra_stats):
        col.metric(label, f"{int(value)}" if pd.notna(value) else "—")

with tabs[2]:
    opp_stats = player_df.groupby("opponent").agg(
        utakmice=("player","count"), PTS=("pts","mean"), REB=("reb","mean"),
        AST=("ast","mean"), **{"3PM": ("3pm", "mean"), "PIR": ("pir", "mean")}
    ).round(2).sort_values("utakmice", ascending=False)
    st.dataframe(opp_stats, use_container_width=True)
    opp_options = sorted(player_df["opponent"].dropna().unique())
    selected_opp = st.selectbox("Detaljna istorija protiv tima", opp_options)
    vs = player_df[player_df["opponent"] == selected_opp]
    st.write(f"**{chosen_player} protiv {selected_opp}:** {len(vs)} utakmica")
    vs_display = vs[["date","pts","reb","ast","3pm","pir","min"]].copy()
    vs_display["date"] = vs_display["date"].map(format_serbian_date)
    st.dataframe(vs_display.rename(
        columns={"date":"Datum","pts":"PTS","reb":"REB","ast":"AST","3pm":"3PM","pir":"PIR","min":"Min"}),
        use_container_width=True, hide_index=True)


with tabs[3]:
    st.subheader("🛡️ Šta protivnik dozvoljava")
    st.caption(
        "Analiza je zasnovana na stvarnim timskim box-score podacima. "
        "Pokazuje šta protivnik dozvoljava u proseku; ne tvrdi da zna da li igra čoveka, zonu, switch ili drop."
    )
    if team_data.empty:
        st.warning("Timski box-score podaci nisu dostupni za ovu sezonu.")
    else:
        defense_teams = sorted(team_data["team"].dropna().unique())
        current_opp = str(latest["opponent"])
        default_idx = defense_teams.index(current_opp) if current_opp in defense_teams else 0
        defense_team = st.selectbox("Izaberi protivničku ekipu", defense_teams, index=default_idx)

        # Defensive team's row identifies the opponent; opponent's row in the same game
        # supplies the actual production allowed by that defense.
        defensive_rows = team_data[team_data["team"] == defense_team].copy()
        allowed_cols = ["pts", "fg2m", "fg2a", "fg3m", "fg3a", "ftm", "fta",
                        "oreb", "dreb", "ast", "tov", "pir"]
        scoring_rows = team_data[["game_id", "team"] + allowed_cols].copy()
        scoring_rows = scoring_rows.rename(columns={"team": "scoring_team"})
        defense_games = defensive_rows.merge(
            scoring_rows,
            left_on=["game_id", "opponent"],
            right_on=["game_id", "scoring_team"],
            suffixes=("_def", "_allowed"),
            how="inner"
        ).sort_values("date", ascending=False)

        if defense_games.empty:
            st.info("Nema dovoljno zajedničkih utakmica za izračun odbrane ove ekipe.")
        else:
            recent_n = st.selectbox("Period odbrane", ["Poslednjih 5", "Poslednjih 10", "Cela sezona"], index=1)
            if recent_n == "Poslednjih 5":
                defense_sample = defense_games.head(5)
            elif recent_n == "Poslednjih 10":
                defense_sample = defense_games.head(10)
            else:
                season_num = int(latest["season"])
                defense_sample = defense_games[defense_games["season"] == season_num]

            if defense_sample.empty:
                defense_sample = defense_games.head(10)

            games_n = len(defense_sample)
            allowed = {
                "PTS": "pts_allowed", "2PM": "fg2m_allowed", "2PA": "fg2a_allowed",
                "3PM": "fg3m_allowed", "3PA": "fg3a_allowed", "FTM": "ftm_allowed",
                "FTA": "fta_allowed", "REB": None, "AST": "ast_allowed",
                "TOV": "tov_allowed", "PIR": "pir_allowed"
            }
            defense_sample["reb_allowed"] = defense_sample["oreb_allowed"] + defense_sample["dreb_allowed"]
            defense_sample["fg_pct_allowed"] = (
                (defense_sample["fg2m_allowed"] + defense_sample["fg3m_allowed"]) /
                (defense_sample["fg2a_allowed"] + defense_sample["fg3a_allowed"]).replace(0, pd.NA) * 100
            )
            defense_sample["3p_pct_allowed"] = (
                defense_sample["fg3m_allowed"] /
                defense_sample["fg3a_allowed"].replace(0, pd.NA) * 100
            )
            defense_sample["3pa_rate_allowed"] = (
                defense_sample["fg3a_allowed"] /
                (defense_sample["fg2a_allowed"] + defense_sample["fg3a_allowed"]).replace(0, pd.NA) * 100
            )

            stat_cards = [
                ("Poeni dozvoljeni", "pts_allowed"),
                ("Skokovi dozvoljeni", "reb_allowed"),
                ("Asistencije dozvoljene", "ast_allowed"),
                ("Trojke dozvoljene", "fg3m_allowed"),
                ("Izgubljene lopte rivala", "tov_allowed"),
            ]
            cards = st.columns(5)
            for card, (label, colname) in zip(cards, stat_cards):
                card.metric(label, f"{defense_sample[colname].mean():.1f}")

            st.markdown("#### Šuterski učinak protivnika")
            shot_cols = st.columns(3)
            shot_cols[0].metric("FG% dozvoljen", f"{defense_sample['fg_pct_allowed'].mean():.1f}%")
            shot_cols[1].metric("3P% dozvoljen", f"{defense_sample['3p_pct_allowed'].mean():.1f}%")
            shot_cols[2].metric("Udeo pokušaja za 3", f"{defense_sample['3pa_rate_allowed'].mean():.1f}%")

            st.markdown("#### Profil odbrane — merljivi pokazatelji")
            league_base = team_data[team_data["season"] == int(defense_sample["season"].iloc[0])].copy()
            league_base = league_base.merge(
                league_base[["game_id", "team"] + allowed_cols].rename(columns={"team":"scoring_team"}),
                left_on=["game_id", "opponent"], right_on=["game_id", "scoring_team"],
                suffixes=("_def", "_allowed")
            )
            league_base["reb_allowed"] = league_base["oreb_allowed"] + league_base["dreb_allowed"]
            comparisons = [
                ("Poeni dozvoljeni", "pts_allowed"),
                ("Skokovi dozvoljeni", "reb_allowed"),
                ("Asistencije dozvoljene", "ast_allowed"),
                ("Trojke dozvoljene", "fg3m_allowed"),
                ("FG% dozvoljen", None),
                ("Udeo pokušaja za 3", None),
            ]
            league_base["fg_pct_allowed"] = (
                (league_base["fg2m_allowed"] + league_base["fg3m_allowed"]) /
                (league_base["fg2a_allowed"] + league_base["fg3a_allowed"]).replace(0, pd.NA) * 100
            )
            league_base["3pa_rate_allowed"] = (
                league_base["fg3a_allowed"] /
                (league_base["fg2a_allowed"] + league_base["fg3a_allowed"]).replace(0, pd.NA) * 100
            )
            compare_cols = [
                ("Poeni dozvoljeni", "pts_allowed"),
                ("Skokovi dozvoljeni", "reb_allowed"),
                ("Asistencije dozvoljene", "ast_allowed"),
                ("Trojke dozvoljene", "fg3m_allowed"),
                ("FG% dozvoljen", "fg_pct_allowed"),
                ("Udeo pokušaja za 3", "3pa_rate_allowed"),
            ]
            rows = []
            for label, metric in compare_cols:
                team_value = float(defense_sample[metric].mean())
                league_value = float(league_base[metric].mean())
                diff = team_value - league_value
                if diff > 0.05:
                    interpretation = "Dozvoljava više od proseka"
                elif diff < -0.05:
                    interpretation = "Dozvoljava manje od proseka"
                else:
                    interpretation = "Oko proseka lige"
                rows.append({"Pokazatelj": label, "Protivnik": round(team_value, 2),
                             "Prosek lige": round(league_value, 2), "Tumačenje": interpretation})
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

            st.markdown("#### Kakvu odbranu pokazuju brojke?")
            style_notes = []
            pts_diff = defense_sample["pts_allowed"].mean() - league_base["pts_allowed"].mean()
            reb_diff = defense_sample["reb_allowed"].mean() - league_base["reb_allowed"].mean()
            ast_diff = defense_sample["ast_allowed"].mean() - league_base["ast_allowed"].mean()
            three_rate_diff = defense_sample["3pa_rate_allowed"].mean() - league_base["3pa_rate_allowed"].mean()
            fg_diff = defense_sample["fg_pct_allowed"].mean() - league_base["fg_pct_allowed"].mean()

            if pts_diff <= -2:
                style_notes.append("Čvrsta odbrana po broju primljenih poena.")
            elif pts_diff >= 2:
                style_notes.append("Rivalima dopušta iznadprosečan broj poena.")
            if three_rate_diff >= 3:
                style_notes.append("Protivnici protiv nje uzimaju veći udeo šuteva za tri — spolja im ostavlja više pokušaja.")
            elif three_rate_diff <= -3:
                style_notes.append("Protivnici uzimaju manji udeo šuteva za tri — ograničava volumen šuta spolja.")
            if fg_diff <= -2:
                style_notes.append("Snižava procenat šuta protivnika; pokazatelj otežanih pokušaja, ne dokaz konkretne šeme.")
            elif fg_diff >= 2:
                style_notes.append("Protivnici protiv nje šutiraju efikasnije od proseka lige.")
            if reb_diff >= 1.5:
                style_notes.append("Dopušta više skokova protivniku, što može ukazivati na problem u zatvaranju skoka.")
            elif reb_diff <= -1.5:
                style_notes.append("Dobro ograničava protivnički skok.")
            if ast_diff <= -1.0:
                style_notes.append("Protivnici beleže manje asistencija — moguće otežava kreiranje otvorenih šuteva.")
            elif ast_diff >= 1.0:
                style_notes.append("Protivnici beleže više asistencija — češće dolaze do timskog protoka lopte.")
            if not style_notes:
                style_notes.append("Brojke su blizu proseka lige; nema izrazitog statističkog odstupanja u ovom uzorku.")
            for note in style_notes:
                st.write("• " + note)

            st.caption(
                f"Uzorak: {games_n} utakmica. Ovo opisuje rezultate odbrane (šta dozvoljava), "
                "ali box score ne otkriva pouzdano taktičku šemu kao što su zona 2-3, switch, drop ili blitz."
            )



st.markdown("### 📈 Granice kladionica za ovog igrača")
db_offers, offers_error = fetch_db_player_offers(chosen_player)
if offers_error:
    st.info(offers_error)
elif not db_offers.empty:
    st.markdown("#### Aktuelne ponude sačuvane u EURO PICKS bazi")
    db_offers = db_offers.rename(columns={
        "market":"Tržište", "line":"Granica", "bookmaker":"Kladionica",
        "over_odds":"OVER kvota", "under_odds":"UNDER kvota",
        "captured_at":"Vreme preuzimanja", "game_id":"ID utakmice"
    })
    # Tehnički podatak o izvoru ostaje u bazi, ali se ne prikazuje korisniku.
    db_offers = db_offers.drop(columns=["source"], errors="ignore")
    st.dataframe(db_offers, use_container_width=True, hide_index=True)
# No empty bookmaker-offer message is shown when there are no current offers.
st.caption("Istorijska analiza učinka")
prop_label = st.selectbox("Statistika za poređenje", ["Poeni", "Skokovi", "Asistencije", "Trojke", "PIR", "PTS + REB", "PTS + AST", "REB + AST", "PRA"])
combo_cols = {"PTS + REB": ("pts", "reb"), "PTS + AST": ("pts", "ast"), "REB + AST": ("reb", "ast"), "PRA": ("pts", "reb", "ast")}
if prop_label in combo_cols:
    prop_col = "_prop_combo"
    player_df[prop_col] = sum(safe_num(player_df[c]) for c in combo_cols[prop_label])
else:
    prop_col = {"Poeni":"pts", "Skokovi":"reb", "Asistencije":"ast", "Trojke":"3pm", "PIR":"pir"}[prop_label]
line = st.number_input("Granica za istorijsko poređenje", min_value=0.5, max_value=100.5, value=15.5, step=0.5)
sample_choice = st.selectbox("Period", ["Poslednjih 5", "Poslednjih 10", "Poslednjih 20", "Sve utakmice"])
sample_n = {"Poslednjih 5":5, "Poslednjih 10":10, "Poslednjih 20":20}
sample = player_df.head(sample_n[sample_choice]) if sample_choice in sample_n else player_df
over_count = int((safe_num(sample[prop_col]) > line).sum())
under_count = int((safe_num(sample[prop_col]) < line).sum())
pushes = int((safe_num(sample[prop_col]) == line).sum())
n = len(sample)
x1, x2, x3 = st.columns(3)
x1.metric("OVER – prebacio", f"{over_count}/{n}", f"{100*over_count/max(1,n):.0f}%")
x2.metric("UNDER – nije prebacio", f"{under_count}/{n}", f"{100*under_count/max(1,n):.0f}%")
x3.metric("Tačno na granici", pushes)
st.write(f"Prosek u uzorku: **{avg(sample, prop_col):.2f}** | Medijana: **{safe_num(sample[prop_col]).median():.1f}**")
sample_display = sample[["date", "opponent", prop_col]].copy()
sample_display["date"] = sample_display["date"].map(format_serbian_date)
st.dataframe(sample_display.rename(columns={"date":"Datum", "opponent":"Protivnik", prop_col:prop_label}), use_container_width=True, hide_index=True)

# Visual summary inspired by the supplied EURO PICKS reference.
best_side = "OVER" if over_count > under_count else "UNDER" if under_count > over_count else "NEUTRAL"
best_rate = max(over_count, under_count) / max(1, n) * 100
best_count = max(over_count, under_count)
best_line_label = "OVER" if best_side == "OVER" else "UNDER"
insight_lines = [
    f"{best_count} od {n} nastupa je na strani {best_side.lower()} granice {line:g}.",
    f"Prosek u uzorku iznosi {avg(sample, prop_col):.1f}, a medijana {safe_num(sample[prop_col]).median():.1f}.",
    f"Tačno na granici: {pushes} nastupa."
]
insights_html = "".join(f'<div class="ep-insight">✓ &nbsp;{html.escape(x)}</div>' for x in insight_lines)
st.markdown(f"""
<div class="ep-panel" style="margin-top:18px">
  <div class="ep-panel-title"><span>✦</span> KEY INSIGHTS</div>
  {insights_html}
</div>
<div class="ep-pick">
  <div><div class="ep-pick-label">{html.escape(prop_label.upper())} • ISTORIJSKI UČINAK</div>
  <div class="ep-pick-main">{html.escape(str(chosen_player).upper())} – {line:g} {html.escape(prop_label.upper())}</div>
  <div class="ep-pick-main" style="color:#25df95;font-size:clamp(32px,5vw,58px)">{best_line_label}</div>
  <div class="ep-muted">Statistički pregled uzorka, nije garancija ishoda niti automatska opklada.</div></div>
  <div class="ep-pick-side"><div class="ep-muted">PROLAZ • {html.escape(sample_choice.upper())}</div>
  <div class="ep-pick-pct">{best_rate:.0f}%</div><div class="ep-muted">{best_count}/{n} utakmica</div></div>
</div>
""", unsafe_allow_html=True)


st.markdown("---")
st.header("🧠 EURO PICKS MODEL — igrači koji nedostaju")
st.write(
    "Ovaj modul pravi sopstvenu procenu za igrača kog nema u bazi. "
    "Unesi njegove proseke iz prethodne lige ili ranijeg perioda; model ih prilagođava "
    "očekivanoj minutaži i nivou takmičenja. Ako nemaš statistiku, prikazuje samo grubi "
    "ligaški reper i jasno označava nisku pouzdanost."
)

with st.expander("Otvori procenu igrača koji nedostaje", expanded=True):
    c1, c2, c3 = st.columns(3)
    with c1:
        missing_name = st.text_input("Ime igrača", value="Novi igrač")
        missing_position = st.selectbox("Pozicija", ["PG", "SG", "SF", "PF", "C"], key="missing_pos")
    with c2:
        expected_minutes = st.slider("Očekivana minutaža", 5, 40, 24)
        source_level = st.selectbox(
            "Liga iz koje dolaze proseci",
            ["EuroLeague", "EuroCup", "ABA", "Ostala evropska liga", "NBA", "Nema prethodnih podataka"],
            key="source_level"
        )
    with c3:
        target_level = st.selectbox(
            "Takmičenje u kom prognoziramo", ["EuroLeague", "EuroCup", "ABA"],
            key="target_level"
        )
        role = st.selectbox(
            "Uloga", ["Starter / glavna opcija", "Starter / sporedna opcija",
                      "Klupa / rotacija", "Klupa / ograničena uloga"]
        )

    st.caption("Unesi proseke po utakmici iz prethodne lige. Ako ih nemaš, ostavi nule.")
    s1, s2, s3, s4, s5 = st.columns(5)
    with s1:
        input_pts = st.number_input("PTS prosek", 0.0, 60.0, 0.0, 0.5)
    with s2:
        input_reb = st.number_input("REB prosek", 0.0, 30.0, 0.0, 0.5)
    with s3:
        input_ast = st.number_input("AST prosek", 0.0, 25.0, 0.0, 0.5)
    with s4:
        input_3pm = st.number_input("3PM prosek", 0.0, 15.0, 0.0, 0.5)
    with s5:
        input_pir = st.number_input("PIR prosek", 0.0, 80.0, 0.0, 0.5)

    d1, d2 = st.columns(2)
    with d1:
        source_games = st.number_input("Broj utakmica u uzorku", 0, 100, 0, 1)
    with d2:
        source_minutes = st.number_input("Prosečna minutaža u izvornoj ligi", 1.0, 40.0, 25.0, 0.5)

    league_factor = {
        "EuroLeague": 1.00, "EuroCup": 0.94, "ABA": 0.90,
        "Ostala evropska liga": 0.86, "NBA": 0.92,
        "Nema prethodnih podataka": 1.00,
    }
    target_factor = {"EuroLeague": 1.00, "EuroCup": 0.94, "ABA": 0.90}
    role_factor = {
        "Starter / glavna opcija": 1.08,
        "Starter / sporedna opcija": 1.00,
        "Klupa / rotacija": 0.90,
        "Klupa / ograničena uloga": 0.78,
    }
    level_adjustment = target_factor[target_level] / league_factor[source_level]
    role_adjustment = role_factor[role]
    minutes_adjustment = expected_minutes / source_minutes

    reference = {}
    for stat in ["pts", "reb", "ast", "3pm", "pir"]:
        eligible = all_data[all_data["min"] >= 5]
        per30 = safe_num(eligible[stat]) / eligible["min"] * 30
        reference[stat] = float(per30.mean()) if len(per30) else 0.0

    input_stats = {"pts": input_pts, "reb": input_reb, "ast": input_ast,
                   "3pm": input_3pm, "pir": input_pir}
    have_history = any(v > 0 for v in input_stats.values())

    if st.button("Izračunaj procenu igrača", type="primary", key="estimate_missing"):
        estimates = {}
        for stat, supplied in input_stats.items():
            prior_per30 = reference[stat]
            if have_history and supplied > 0:
                source_per30 = supplied / source_minutes * 30
                sample_weight = min(0.85, source_games / (source_games + 8.0)) if source_games > 0 else 0.50
                adjusted_per30 = sample_weight * source_per30 + (1.0 - sample_weight) * prior_per30
            else:
                adjusted_per30 = prior_per30
            estimates[stat] = max(
                0.0, adjusted_per30 * (expected_minutes / 30.0) *
                level_adjustment * role_adjustment
            )

        confidence = "NISKA"
        if have_history and source_games >= 15:
            confidence = "SREDNJA"
        elif have_history and source_games >= 5:
            confidence = "NISKA–SREDNJA"

        st.subheader(f"Procena: {missing_name}")
        st.caption(f"{missing_position} • {target_level} • {expected_minutes} očekivanih minuta • Pouzdanost: {confidence}")
        mcols = st.columns(5)
        for col, label, stat in zip(mcols, ["PTS", "REB", "AST", "3PM", "PIR"],
                                    ["pts", "reb", "ast", "3pm", "pir"]):
            col.metric(label, f"{estimates[stat]:.1f}")

        if not have_history:
            st.warning(
                "Nema unetih statistika igrača. Ovo je samo opšti reper iz trenutno učitane baze, "
                "ne individualna prognoza. Ne koristimo poziciju kao da znamo stvarni učinak."
            )
        else:
            st.info(
                "Procena koristi unete proseke, minutažu, broj utakmica i početne korekcije "
                "takmičenja i uloge. Korekcije liga su privremene pretpostavke, a ne zvanični "
                "koeficijenti niti model istreniran na ABA/EuroCup podacima."
            )
        st.write(
            f"**Pretpostavke:** korekcija takmičenja {level_adjustment:.2f}× · "
            f"korekcija uloge {role_adjustment:.2f}× · minutaža {minutes_adjustment:.2f}×"
        )
        st.caption(
            "Procena nije stvarna statistika niti garancija ishoda. Za sledeću iteraciju treba "
            "dodati stvarne box score podatke iz ABA/EuroCup i testirati prognoze na ranijim utakmicama."
        )


if load_errors:
    with st.expander("Upozorenja pri preuzimanju sezona"):
        st.write("\n".join(load_errors))


with tabs[4]:
    show_absence_impact(sb, chosen_player)
