use serde::Serialize;
use tauri::AppHandle;

#[derive(Clone, Debug, Serialize)]
pub struct DeviceLocationFix {
    pub latitude: f64,
    pub longitude: f64,
    pub accuracy_m: Option<f64>,
    pub captured_at: String,
}

#[tauri::command]
pub async fn get_device_location(app: AppHandle) -> Result<DeviceLocationFix, String> {
    platform::request_fix(app).await
}

#[cfg(target_os = "macos")]
mod platform {
    use super::DeviceLocationFix;
    use chrono::{TimeZone, Utc};
    use objc2::rc::Retained;
    use objc2::runtime::{NSObject, NSObjectProtocol, ProtocolObject};
    use objc2::{define_class, msg_send, DefinedClass, MainThreadOnly};
    use objc2_core_location::{
        kCLLocationAccuracyHundredMeters, CLAuthorizationStatus, CLError, CLLocation,
        CLLocationManager, CLLocationManagerDelegate,
    };
    use objc2_foundation::{MainThreadMarker, NSArray, NSError};
    use std::cell::Cell;
    use std::sync::Mutex;
    use std::time::Duration;
    use tauri::AppHandle;
    use tokio::sync::oneshot;

    struct LocationDelegateIvars {
        result_tx: Mutex<Option<oneshot::Sender<Result<DeviceLocationFix, String>>>>,
        requested_location: Cell<bool>,
    }

    define_class!(
        #[unsafe(super(NSObject))]
        #[thread_kind = MainThreadOnly]
        #[name = "JarvisLocationDelegate"]
        #[ivars = LocationDelegateIvars]
        struct LocationDelegate;

        unsafe impl NSObjectProtocol for LocationDelegate {}

        unsafe impl CLLocationManagerDelegate for LocationDelegate {
            #[unsafe(method(locationManagerDidChangeAuthorization:))]
            fn did_change_authorization(&self, manager: &CLLocationManager) {
                self.handle_authorization(manager);
            }

            #[unsafe(method(locationManager:didUpdateLocations:))]
            fn did_update_locations(
                &self,
                _manager: &CLLocationManager,
                locations: &NSArray<CLLocation>,
            ) {
                let Some(location) = locations.lastObject() else {
                    self.finish(Err("unavailable".into()));
                    return;
                };
                self.finish(Ok(fix_from_cl(&location)));
            }

            #[unsafe(method(locationManager:didFailWithError:))]
            fn did_fail_with_error(&self, _manager: &CLLocationManager, error: &NSError) {
                let code = CLError(error.code());
                let reason = match code {
                    CLError::Denied | CLError::PromptDeclined => "denied",
                    _ => "unavailable",
                };
                self.finish(Err(reason.into()));
            }
        }
    );

    impl LocationDelegate {
        fn new(
            mtm: MainThreadMarker,
            result_tx: oneshot::Sender<Result<DeviceLocationFix, String>>,
        ) -> Retained<Self> {
            let this = mtm.alloc::<Self>().set_ivars(LocationDelegateIvars {
                result_tx: Mutex::new(Some(result_tx)),
                requested_location: Cell::new(false),
            });
            unsafe { msg_send![super(this), init] }
        }

        fn handle_authorization(&self, manager: &CLLocationManager) {
            let status = unsafe { manager.authorizationStatus() };
            match status {
                CLAuthorizationStatus::NotDetermined => unsafe {
                    manager.requestWhenInUseAuthorization();
                },
                CLAuthorizationStatus::AuthorizedAlways
                | CLAuthorizationStatus::AuthorizedWhenInUse => {
                    if !self.ivars().requested_location.replace(true) {
                        unsafe {
                            manager.requestLocation();
                        }
                    }
                }
                CLAuthorizationStatus::Denied | CLAuthorizationStatus::Restricted => {
                    self.finish(Err("denied".into()));
                }
                _ => self.finish(Err("unavailable".into())),
            }
        }

        fn finish(&self, result: Result<DeviceLocationFix, String>) {
            if let Some(tx) = self
                .ivars()
                .result_tx
                .lock()
                .unwrap_or_else(|error| error.into_inner())
                .take()
            {
                let _ = tx.send(result);
            }
        }
    }

    struct KeepAlive {
        _manager: Retained<CLLocationManager>,
        _delegate: Retained<LocationDelegate>,
    }

    // Retained Objective-C objects are only touched on the main thread after setup.
    unsafe impl Send for KeepAlive {}

    pub async fn request_fix(app: AppHandle) -> Result<DeviceLocationFix, String> {
        let (result_tx, result_rx) = oneshot::channel();
        let (keep_tx, keep_rx) = oneshot::channel::<KeepAlive>();

        app.run_on_main_thread(move || {
            let Some(mtm) = MainThreadMarker::new() else {
                let _ = result_tx.send(Err("unavailable".into()));
                return;
            };

            if !unsafe { CLLocationManager::locationServicesEnabled_class() } {
                let _ = result_tx.send(Err("unavailable".into()));
                return;
            }

            let manager = unsafe { CLLocationManager::new() };
            unsafe {
                manager.setDesiredAccuracy(kCLLocationAccuracyHundredMeters);
            }

            let delegate = LocationDelegate::new(mtm, result_tx);
            unsafe {
                manager.setDelegate(Some(ProtocolObject::from_ref(&*delegate)));
            }

            // Drive the first authorization decision immediately; the delegate also
            // receives locationManagerDidChangeAuthorization: when status changes.
            delegate.handle_authorization(&manager);

            let _ = keep_tx.send(KeepAlive {
                _manager: manager,
                _delegate: delegate,
            });
        })
        .map_err(|error| format!("unavailable: {error}"))?;

        // Allow time for the system authorization dialog plus a one-shot fix.
        let keep = keep_rx.await.ok();
        let result = tokio::time::timeout(Duration::from_secs(60), result_rx)
            .await
            .map_err(|_| "timeout".to_string())?
            .map_err(|_| "unavailable".to_string())?;
        drop(keep);
        result
    }

    fn fix_from_cl(location: &CLLocation) -> DeviceLocationFix {
        let coordinate = unsafe { location.coordinate() };
        let accuracy = unsafe { location.horizontalAccuracy() };
        let timestamp = unsafe { location.timestamp().timeIntervalSince1970() };
        let secs = timestamp.trunc() as i64;
        let nanos = ((timestamp.fract().abs() * 1_000_000_000.0) as u32).min(999_999_999);
        let captured_at = Utc
            .timestamp_opt(secs, nanos)
            .single()
            .unwrap_or_else(Utc::now)
            .to_rfc3339();

        DeviceLocationFix {
            latitude: coordinate.latitude,
            longitude: coordinate.longitude,
            accuracy_m: if accuracy >= 0.0 {
                Some(accuracy)
            } else {
                None
            },
            captured_at,
        }
    }
}

#[cfg(not(target_os = "macos"))]
mod platform {
    use super::DeviceLocationFix;
    use tauri::AppHandle;

    pub async fn request_fix(_app: AppHandle) -> Result<DeviceLocationFix, String> {
        Err("unavailable".into())
    }
}
