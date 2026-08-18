use serde::Serialize;
use std::sync::{Arc, Mutex};
use tauri::{AppHandle, Emitter, State};

#[derive(Clone, Debug, Default, PartialEq, Serialize)]
pub struct CallActivity {
    pub active: bool,
    pub app: Option<String>,
    pub supported: bool,
}

pub type SharedCallActivity = Arc<Mutex<CallActivity>>;

pub fn shared_call_activity() -> SharedCallActivity {
    Arc::new(Mutex::new(CallActivity::default()))
}

#[tauri::command]
pub fn get_call_activity(activity: State<'_, SharedCallActivity>) -> CallActivity {
    activity
        .lock()
        .unwrap_or_else(|error| error.into_inner())
        .clone()
}

pub fn start_call_activity_monitor(app: AppHandle, activity: SharedCallActivity) {
    tauri::async_runtime::spawn(async move {
        let mut inactive_scans = 0;
        loop {
            let scan = platform::active_conference();
            let mut next = activity
                .lock()
                .unwrap_or_else(|error| error.into_inner())
                .clone();

            match scan {
                Ok(Some(app_name)) => {
                    inactive_scans = 0;
                    next = CallActivity {
                        active: true,
                        app: Some(app_name),
                        supported: true,
                    };
                }
                Ok(None) => {
                    next.supported = true;
                    if next.active {
                        inactive_scans += 1;
                        if inactive_scans >= 3 {
                            next.active = false;
                            next.app = None;
                            inactive_scans = 0;
                        }
                    }
                }
                Err(()) => {
                    next = CallActivity::default();
                    inactive_scans = 0;
                }
            }

            let changed = {
                let mut current = activity.lock().unwrap_or_else(|error| error.into_inner());
                if *current == next {
                    false
                } else {
                    *current = next.clone();
                    true
                }
            };
            if changed {
                let _ = app.emit("call-activity-update", next);
            }

            tokio::time::sleep(std::time::Duration::from_secs(1)).await;
        }
    });
}

#[cfg(target_os = "macos")]
mod platform {
    use core_foundation::{
        base::TCFType,
        string::{CFString, CFStringRef},
    };
    use coreaudio_sys::{
        kAudioHardwarePropertyProcessObjectList, kAudioObjectPropertyElementMain,
        kAudioObjectPropertyScopeGlobal, kAudioObjectSystemObject, kAudioProcessPropertyBundleID,
        kAudioProcessPropertyIsRunningInput, kAudioProcessPropertyIsRunningOutput,
        AudioObjectGetPropertyData, AudioObjectGetPropertyDataSize, AudioObjectID,
        AudioObjectPropertyAddress,
    };
    use std::{ffi::c_void, mem};

    pub fn active_conference() -> Result<Option<String>, ()> {
        let process_ids = process_ids()?;
        for process_id in process_ids {
            let Some(bundle_id) = bundle_id(process_id) else {
                continue;
            };
            let Some(app_name) = conference_app_name(&bundle_id) else {
                continue;
            };
            if property_u32(process_id, kAudioProcessPropertyIsRunningInput) == Some(1)
                && property_u32(process_id, kAudioProcessPropertyIsRunningOutput) == Some(1)
            {
                return Ok(Some(app_name.to_string()));
            }
        }
        Ok(None)
    }

    fn process_ids() -> Result<Vec<AudioObjectID>, ()> {
        let address = property_address(kAudioHardwarePropertyProcessObjectList);
        let mut byte_size = 0;
        let status = unsafe {
            AudioObjectGetPropertyDataSize(
                kAudioObjectSystemObject,
                &address,
                0,
                std::ptr::null(),
                &mut byte_size,
            )
        };
        if status != 0 {
            return Err(());
        }

        let count = byte_size as usize / mem::size_of::<AudioObjectID>();
        let mut process_ids = vec![0; count];
        let status = unsafe {
            AudioObjectGetPropertyData(
                kAudioObjectSystemObject,
                &address,
                0,
                std::ptr::null(),
                &mut byte_size,
                process_ids.as_mut_ptr().cast::<c_void>(),
            )
        };
        (status == 0).then_some(process_ids).ok_or(())
    }

    fn property_u32(object_id: AudioObjectID, selector: u32) -> Option<u32> {
        let address = property_address(selector);
        let mut value = 0_u32;
        let mut byte_size = mem::size_of::<u32>() as u32;
        let status = unsafe {
            AudioObjectGetPropertyData(
                object_id,
                &address,
                0,
                std::ptr::null(),
                &mut byte_size,
                (&mut value as *mut u32).cast::<c_void>(),
            )
        };
        (status == 0).then_some(value)
    }

    fn bundle_id(object_id: AudioObjectID) -> Option<String> {
        let address = property_address(kAudioProcessPropertyBundleID);
        let mut value: CFStringRef = std::ptr::null();
        let mut byte_size = mem::size_of::<CFStringRef>() as u32;
        let status = unsafe {
            AudioObjectGetPropertyData(
                object_id,
                &address,
                0,
                std::ptr::null(),
                &mut byte_size,
                (&mut value as *mut CFStringRef).cast::<c_void>(),
            )
        };
        if status != 0 || value.is_null() {
            return None;
        }
        Some(unsafe { CFString::wrap_under_create_rule(value) }.to_string())
    }

    fn property_address(selector: u32) -> AudioObjectPropertyAddress {
        AudioObjectPropertyAddress {
            mSelector: selector,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain,
        }
    }

    fn conference_app_name(bundle_id: &str) -> Option<&'static str> {
        let normalized = bundle_id.to_ascii_lowercase();
        if normalized == "us.zoom.xos" || normalized.starts_with("us.zoom.") {
            return Some("Zoom");
        }
        if normalized.starts_with("com.microsoft.teams") {
            return Some("Microsoft Teams");
        }
        if normalized == "com.apple.facetime" {
            return Some("FaceTime");
        }
        if normalized.contains("webex") {
            return Some("Webex");
        }
        if normalized.starts_with("com.tinyspeck.slackmacgap") {
            return Some("Slack");
        }
        if normalized.starts_with("com.hnc.discord") {
            return Some("Discord");
        }
        None
    }

    #[cfg(test)]
    mod tests {
        use super::conference_app_name;

        #[test]
        fn recognizes_supported_conferencing_apps() {
            assert_eq!(conference_app_name("us.zoom.xos"), Some("Zoom"));
            assert_eq!(
                conference_app_name("com.microsoft.teams2"),
                Some("Microsoft Teams")
            );
            assert_eq!(conference_app_name("com.apple.FaceTime"), Some("FaceTime"));
            assert_eq!(
                conference_app_name("com.cisco.webexmeetingsapp"),
                Some("Webex")
            );
        }

        #[test]
        fn ignores_unrelated_audio_processes() {
            assert_eq!(conference_app_name("com.apple.Music"), None);
            assert_eq!(conference_app_name("dev.jarv1s.host"), None);
        }
    }
}

#[cfg(not(target_os = "macos"))]
mod platform {
    pub fn active_conference() -> Result<Option<String>, ()> {
        Err(())
    }
}
