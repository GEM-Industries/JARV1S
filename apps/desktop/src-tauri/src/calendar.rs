//! Loopback EventKit API for the Python calendar provider.
//!
//! TCC Calendar access is requested from this Host process (dev.jarv1s.host).
//! A dedicated owner thread holds the long-lived EKEventStore; the permission
//! prompt can present because the Host is a real NSApplication.

use serde::Serialize;
use tokio::sync::oneshot;

#[derive(Clone, Debug)]
pub struct HostCalendar {
    pub url: String,
    pub token: String,
}

pub struct HostCalendarServer {
    pub handle: HostCalendar,
    shutdown: Option<oneshot::Sender<()>>,
}

impl HostCalendarServer {
    pub fn shutdown(&mut self) {
        if let Some(tx) = self.shutdown.take() {
            let _ = tx.send(());
        }
    }
}

pub async fn start() -> Option<HostCalendarServer> {
    platform::start().await
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum AccessStatus {
    Authorized,
    Denied,
    NotDetermined,
    Restricted,
}

impl AccessStatus {
    fn as_str(self) -> &'static str {
        match self {
            Self::Authorized => "authorized",
            Self::Denied => "denied",
            Self::NotDetermined => "notDetermined",
            Self::Restricted => "restricted",
        }
    }
}

#[derive(Debug, Serialize)]
struct EventJson {
    id: String,
    title: String,
    start: String,
    end: String,
    location: Option<String>,
    description: Option<String>,
    is_all_day: bool,
    attendees: Vec<String>,
    calendar: Option<String>,
    recurrence: Option<String>,
}

enum CalendarOp {
    Status(oneshot::Sender<AccessStatus>),
    Authorize(oneshot::Sender<AccessStatus>),
    Events {
        time_min: String,
        time_max: String,
        event_id: Option<String>,
        reply: oneshot::Sender<Result<Vec<EventJson>, String>>,
    },
}

#[cfg(target_os = "macos")]
mod platform {
    use super::*;
    use std::collections::HashMap;
    use std::io::Read;
    use std::time::Duration;

    use chrono::TimeZone;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::TcpListener;

    pub async fn start() -> Option<HostCalendarServer> {
        let token = match process_token() {
            Ok(token) => token,
            Err(error) => {
                eprintln!("host calendar token generation failed: {error}");
                return None;
            }
        };

        let (op_tx, op_rx) = std::sync::mpsc::channel::<CalendarOp>();
        std::thread::Builder::new()
            .name("jarvis-eventkit".into())
            .spawn(move || eventkit_owner(op_rx))
            .ok()?;

        let listener = match TcpListener::bind("127.0.0.1:0").await {
            Ok(listener) => listener,
            Err(error) => {
                eprintln!("host calendar bind failed: {error}");
                return None;
            }
        };
        let port = listener.local_addr().ok()?.port();
        let url = format!("http://127.0.0.1:{port}");
        let (shutdown_tx, shutdown_rx) = oneshot::channel();
        let serve_token = token.clone();
        tokio::spawn(async move {
            serve(listener, serve_token, op_tx, shutdown_rx).await;
        });

        Some(HostCalendarServer {
            handle: HostCalendar { url, token },
            shutdown: Some(shutdown_tx),
        })
    }

    fn process_token() -> std::io::Result<String> {
        let mut bytes = [0_u8; 32];
        std::fs::File::open("/dev/urandom")?.read_exact(&mut bytes)?;
        let mut token = String::with_capacity(bytes.len() * 2);
        for byte in bytes {
            use std::fmt::Write as _;
            let _ = write!(token, "{byte:02x}");
        }
        Ok(token)
    }

    async fn serve(
        listener: TcpListener,
        token: String,
        ops: std::sync::mpsc::Sender<CalendarOp>,
        mut shutdown: oneshot::Receiver<()>,
    ) {
        loop {
            tokio::select! {
                _ = &mut shutdown => return,
                accepted = listener.accept() => {
                    let Ok((mut stream, _)) = accepted else { continue };
                    let token = token.clone();
                    let ops = ops.clone();
                    tokio::spawn(async move {
                        let _ = handle_connection(&mut stream, &token, &ops).await;
                    });
                }
            }
        }
    }

    async fn handle_connection(
        stream: &mut tokio::net::TcpStream,
        token: &str,
        ops: &std::sync::mpsc::Sender<CalendarOp>,
    ) -> std::io::Result<()> {
        let mut buf = vec![0_u8; 8192];
        let n = stream.read(&mut buf).await?;
        if n == 0 {
            return Ok(());
        }
        let raw = String::from_utf8_lossy(&buf[..n]);
        let (status, body) = dispatch_http(&raw, token, ops).await;
        let response = format!(
            "HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
            body.len()
        );
        stream.write_all(response.as_bytes()).await?;
        Ok(())
    }

    async fn dispatch_http(
        raw: &str,
        token: &str,
        ops: &std::sync::mpsc::Sender<CalendarOp>,
    ) -> (u16, String) {
        let Some(header_end) = raw.find("\r\n\r\n") else {
            return (400, r#"{"error":"bad_request"}"#.into());
        };
        let header = &raw[..header_end];
        let mut lines = header.split("\r\n");
        let request_line = lines.next().unwrap_or("");
        let mut parts = request_line.split_whitespace();
        let method = parts.next().unwrap_or("");
        let path_q = parts.next().unwrap_or("/");
        let (path, query) = match path_q.split_once('?') {
            Some((path, query)) => (path, query),
            None => (path_q, ""),
        };

        let mut authorized = false;
        for line in lines {
            let Some((name, value)) = line.split_once(':') else {
                continue;
            };
            if name.eq_ignore_ascii_case("authorization")
                && value.trim() == format!("Bearer {token}")
            {
                authorized = true;
            }
        }
        if !authorized {
            return (401, r#"{"error":"unauthorized"}"#.into());
        }

        match (method, path) {
            ("GET", "/status") => {
                let (tx, rx) = oneshot::channel();
                if ops.send(CalendarOp::Status(tx)).is_err() {
                    return (503, r#"{"error":"unavailable"}"#.into());
                }
                match rx.await {
                    Ok(status) => (
                        200,
                        serde_json::json!({ "status": status.as_str() }).to_string(),
                    ),
                    Err(_) => (503, r#"{"error":"unavailable"}"#.into()),
                }
            }
            ("POST", "/authorize") => {
                let (tx, rx) = oneshot::channel();
                if ops.send(CalendarOp::Authorize(tx)).is_err() {
                    return (503, r#"{"error":"unavailable"}"#.into());
                }
                match tokio::time::timeout(Duration::from_secs(120), rx).await {
                    Ok(Ok(status)) => (
                        200,
                        serde_json::json!({ "status": status.as_str() }).to_string(),
                    ),
                    Ok(Err(_)) => (503, r#"{"error":"unavailable"}"#.into()),
                    Err(_) => (504, r#"{"error":"timeout"}"#.into()),
                }
            }
            ("GET", "/events") => {
                let params = parse_query(query);
                let Some(time_min) = params.get("time_min").cloned() else {
                    return (400, r#"{"error":"time_min required"}"#.into());
                };
                let Some(time_max) = params.get("time_max").cloned() else {
                    return (400, r#"{"error":"time_max required"}"#.into());
                };
                let event_id = params.get("id").cloned().filter(|value| !value.is_empty());
                let (tx, rx) = oneshot::channel();
                if ops
                    .send(CalendarOp::Events {
                        time_min,
                        time_max,
                        event_id,
                        reply: tx,
                    })
                    .is_err()
                {
                    return (503, r#"{"error":"unavailable"}"#.into());
                }
                match tokio::time::timeout(Duration::from_secs(30), rx).await {
                    Ok(Ok(Ok(events))) => (
                        200,
                        serde_json::json!({ "events": events }).to_string(),
                    ),
                    Ok(Ok(Err(error))) => {
                        let status = if error == "permission_denied" { 403 } else { 400 };
                        (
                            status,
                            serde_json::json!({ "error": error }).to_string(),
                        )
                    }
                    Ok(Err(_)) => (503, r#"{"error":"unavailable"}"#.into()),
                    Err(_) => (504, r#"{"error":"timeout"}"#.into()),
                }
            }
            _ => (404, r#"{"error":"not_found"}"#.into()),
        }
    }

    fn parse_query(query: &str) -> HashMap<String, String> {
        let mut out = HashMap::new();
        for pair in query.split('&') {
            if pair.is_empty() {
                continue;
            }
            let (key, value) = pair.split_once('=').unwrap_or((pair, ""));
            out.insert(
                urlencoding::decode(key).unwrap_or_default().into_owned(),
                urlencoding::decode(value).unwrap_or_default().into_owned(),
            );
        }
        out
    }

    fn eventkit_owner(rx: std::sync::mpsc::Receiver<CalendarOp>) {
        use block2::RcBlock;
        use objc2::runtime::Bool;
        use objc2_event_kit::{
            EKAuthorizationStatus, EKCalendarItem, EKEntityType, EKEvent, EKEventStore,
            EKRecurrenceFrequency, EKRecurrenceRule,
        };
        use objc2_foundation::{NSArray, NSDate, NSString};

        let store = unsafe { EKEventStore::new() };

        while let Ok(op) = rx.recv() {
            match op {
                CalendarOp::Status(reply) => {
                    let _ = reply.send(current_status());
                }
                CalendarOp::Authorize(reply) => {
                    let status = current_status();
                    if status != AccessStatus::NotDetermined {
                        let _ = reply.send(status);
                        continue;
                    }
                    request_access(&store);
                    let _ = reply.send(current_status());
                }
                CalendarOp::Events {
                    time_min,
                    time_max,
                    event_id,
                    reply,
                } => {
                    if current_status() == AccessStatus::NotDetermined {
                        request_access(&store);
                    }
                    if current_status() != AccessStatus::Authorized {
                        let _ = reply.send(Err("permission_denied".into()));
                        continue;
                    }
                    let result = fetch_events(&store, &time_min, &time_max, event_id.as_deref());
                    let _ = reply.send(result);
                }
            }
        }

        fn request_access(store: &EKEventStore) {
            // Issue the TCC prompt on the main thread so NSApplication can present
            // it. Wait on this owner thread; do not block main on the reply.
            let (done_tx, done_rx) = std::sync::mpsc::channel();
            let block = RcBlock::new(move |granted: Bool, _error: *mut objc2_foundation::NSError| {
                let _ = done_tx.send(granted.as_bool());
            });
            let block_ptr = RcBlock::as_ptr(&block) as usize;
            let store_ptr = store as *const EKEventStore as usize;
            dispatch2::run_on_main(move |_mtm| {
                let store = unsafe { &*(store_ptr as *const EKEventStore) };
                unsafe {
                    store.requestFullAccessToEventsWithCompletion(block_ptr as *mut _);
                }
            });
            let _ = done_rx.recv_timeout(Duration::from_secs(120));
            unsafe {
                store.reset();
            }
        }

        fn current_status() -> AccessStatus {
            let status =
                unsafe { EKEventStore::authorizationStatusForEntityType(EKEntityType::Event) };
            if status == EKAuthorizationStatus::FullAccess {
                AccessStatus::Authorized
            } else if status == EKAuthorizationStatus::Denied {
                AccessStatus::Denied
            } else if status == EKAuthorizationStatus::Restricted {
                AccessStatus::Restricted
            } else {
                AccessStatus::NotDetermined
            }
        }

        fn ns_date(iso: &str) -> Option<objc2::rc::Retained<NSDate>> {
            let timestamp = if let Ok(dt) = chrono::DateTime::parse_from_rfc3339(iso) {
                dt.timestamp() as f64
            } else if let Ok(naive) =
                chrono::NaiveDateTime::parse_from_str(iso, "%Y-%m-%dT%H:%M:%S")
            {
                naive.and_utc().timestamp() as f64
            } else if let Ok(date) =
                chrono::NaiveDate::parse_from_str(&iso[..iso.len().min(10)], "%Y-%m-%d")
            {
                date.and_hms_opt(0, 0, 0)?.and_utc().timestamp() as f64
            } else {
                return None;
            };
            Some(NSDate::dateWithTimeIntervalSince1970(timestamp))
        }

        fn format_date(date: &NSDate, all_day: bool) -> String {
            let ts = date.timeIntervalSince1970();
            let secs = ts.trunc() as i64;
            let nanos = ((ts.fract().abs() * 1_000_000_000.0) as u32).min(999_999_999);
            let utc = chrono::Utc
                .timestamp_opt(secs, nanos)
                .single()
                .unwrap_or_else(chrono::Utc::now);
            if all_day {
                utc.with_timezone(&chrono::Local)
                    .date_naive()
                    .format("%Y-%m-%d")
                    .to_string()
            } else {
                utc.to_rfc3339()
            }
        }

        fn ns_string(value: Option<objc2::rc::Retained<NSString>>) -> Option<String> {
            value.map(|s| s.to_string()).filter(|s| !s.is_empty())
        }

        fn event_id(event: &EKEvent) -> String {
            let external = unsafe { EKCalendarItem::calendarItemExternalIdentifier(event) };
            let calendar_id = unsafe { event.calendar() }
                .map(|cal| unsafe { cal.calendarIdentifier() }.to_string())
                .unwrap_or_default();
            let start = unsafe { event.startDate() };
            let start_s = format_date(&start, unsafe { event.isAllDay() });
            format!(
                "{}|{calendar_id}|{start_s}",
                ns_string(external).unwrap_or_default()
            )
        }

        fn recurrence_of(event: &EKEvent) -> Option<String> {
            let rules = unsafe { EKCalendarItem::recurrenceRules(event) }?;
            let rule: objc2::rc::Retained<EKRecurrenceRule> = rules.firstObject()?;
            let freq = unsafe { rule.frequency() };
            if freq == EKRecurrenceFrequency::Daily {
                Some("daily".into())
            } else if freq == EKRecurrenceFrequency::Weekly {
                Some("weekly".into())
            } else if freq == EKRecurrenceFrequency::Monthly {
                Some("monthly".into())
            } else if freq == EKRecurrenceFrequency::Yearly {
                Some("yearly".into())
            } else {
                None
            }
        }

        fn to_json(event: &EKEvent) -> EventJson {
            let all_day = unsafe { event.isAllDay() };
            let start = unsafe { event.startDate() };
            let end = unsafe { event.endDate() };
            let calendar = unsafe { event.calendar() };
            let attendees = unsafe { EKCalendarItem::attendees(event) }
                .map(|list| {
                    let mut names = Vec::new();
                    for participant in list {
                        if let Some(name) = ns_string(unsafe { participant.name() }) {
                            names.push(name);
                        }
                    }
                    names
                })
                .unwrap_or_default();
            let title = unsafe { event.title() }.to_string();
            EventJson {
                id: event_id(event),
                title: if title.is_empty() {
                    "(No title)".into()
                } else {
                    title
                },
                start: format_date(&start, all_day),
                end: format_date(&end, all_day),
                location: ns_string(unsafe { EKCalendarItem::location(event) }),
                description: ns_string(unsafe { EKCalendarItem::notes(event) }),
                is_all_day: all_day,
                attendees,
                calendar: calendar.as_ref().and_then(|cal| {
                    let title = unsafe { cal.title() }.to_string();
                    if title.is_empty() {
                        None
                    } else {
                        Some(title)
                    }
                }),
                recurrence: recurrence_of(event),
            }
        }

        fn fetch_events(
            store: &EKEventStore,
            time_min: &str,
            time_max: &str,
            event_id: Option<&str>,
        ) -> Result<Vec<EventJson>, String> {
            let Some(start) = ns_date(time_min) else {
                return Err("invalid_time_min".into());
            };
            let Some(end) = ns_date(time_max) else {
                return Err("invalid_time_max".into());
            };
            let predicate = unsafe {
                store.predicateForEventsWithStartDate_endDate_calendars(&start, &end, None)
            };
            let events: objc2::rc::Retained<NSArray<EKEvent>> =
                unsafe { store.eventsMatchingPredicate(&predicate) };
            let mut out = Vec::new();
            for event in events {
                let json = to_json(&event);
                if let Some(wanted) = event_id {
                    if json.id != wanted {
                        continue;
                    }
                }
                out.push(json);
            }
            Ok(out)
        }
    }
}

#[cfg(not(target_os = "macos"))]
mod platform {
    use super::HostCalendarServer;

    pub async fn start() -> Option<HostCalendarServer> {
        None
    }
}
