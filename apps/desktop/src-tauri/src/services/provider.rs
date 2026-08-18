use super::bundled::{self, BundledServices};
use super::docker;
use crate::paths::HostPaths;
use crate::runtime::{RuntimeLayout, RuntimeMode};
use std::path::{Path, PathBuf};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ServiceProviderKind {
    Docker,
    Bundled,
}

#[derive(Debug, Clone)]
pub struct BackendServiceEnv {
    pub mongodb_url: Option<String>,
}

pub enum ActiveServices {
    None,
    Docker,
    Bundled(BundledServices),
}

pub fn resolve_provider_kind(layout: &RuntimeLayout) -> ServiceProviderKind {
    match std::env::var("JARVIS_SERVICE_PROVIDER").as_deref() {
        Ok("docker") => ServiceProviderKind::Docker,
        Ok("bundled") => ServiceProviderKind::Bundled,
        _ => match layout.mode {
            RuntimeMode::PackagedRuntime => ServiceProviderKind::Bundled,
            RuntimeMode::DevRepo => ServiceProviderKind::Docker,
        },
    }
}

pub fn check_prerequisites(
    kind: ServiceProviderKind,
    layout: &RuntimeLayout,
) -> Result<(), String> {
    match kind {
        ServiceProviderKind::Docker => docker::check_prerequisites(),
        ServiceProviderKind::Bundled => bundled::check_prerequisites(layout),
    }
}

pub async fn start_services(
    kind: ServiceProviderKind,
    layout: &RuntimeLayout,
    paths: &HostPaths,
) -> Result<(ActiveServices, BackendServiceEnv), String> {
    match kind {
        ServiceProviderKind::Docker => {
            docker::start(layout)?;
            Ok((
                ActiveServices::Docker,
                BackendServiceEnv { mongodb_url: None },
            ))
        }
        ServiceProviderKind::Bundled => {
            let bundled = bundled::start(layout, paths).await?;
            let env = BackendServiceEnv {
                mongodb_url: Some(bundled.mongodb_url.clone()),
            };
            Ok((ActiveServices::Bundled(bundled), env))
        }
    }
}

pub async fn stop_services(services: &mut ActiveServices) {
    match services {
        ActiveServices::None => {}
        ActiveServices::Docker => {
            // Docker containers are intentionally left running (Phase 1a behavior).
        }
        ActiveServices::Bundled(bundled) => bundled::stop(bundled).await,
    }
    *services = ActiveServices::None;
}

pub fn services_root(host_root: &Path) -> PathBuf {
    host_root.join("services")
}
