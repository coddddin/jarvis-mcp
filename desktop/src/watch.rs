//! When the server stops on its own, restart it with growing waits, and give up after too many failures in a row.
use std::time::Duration;

/// Waits before restart attempts 1..=5. After the fifth failure in a row the app stops trying.
pub const DELAYS: [Duration; 5] = [
    Duration::from_secs(1),
    Duration::from_secs(2),
    Duration::from_secs(4),
    Duration::from_secs(8),
    Duration::from_secs(16),
];
/// A server that ran this long was healthy: its exit starts a fresh count.
pub const HEALTHY: Duration = Duration::from_secs(60);

#[derive(Debug, PartialEq, Eq)]
pub enum Next {
    Retry { attempt: usize, delay: Duration },
    GiveUp,
}

#[derive(Default)]
pub struct Restarts {
    failures: usize,
}

impl Restarts {
    /// The server was running and ended after `uptime`.
    pub fn after_exit(&mut self, uptime: Duration) -> Next {
        if uptime >= HEALTHY {
            self.failures = 0;
        }
        self.fail()
    }

    /// A restart did not reach READY.
    pub fn after_start_failure(&mut self) -> Next {
        self.fail()
    }

    /// The user asked for a restart after the app gave up.
    pub fn reset(&mut self) {
        self.failures = 0;
    }

    fn fail(&mut self) -> Next {
        match DELAYS.get(self.failures) {
            Some(&delay) => {
                self.failures += 1;
                Next::Retry { attempt: self.failures, delay }
            }
            None => Next::GiveUp,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const QUICK: Duration = Duration::from_secs(3);

    #[test]
    fn quick_failures_back_off_then_give_up_after_five() {
        let mut restarts = Restarts::default();
        let mut waits = Vec::new();
        for _ in 0..5 {
            match restarts.after_exit(QUICK) {
                Next::Retry { attempt, delay } => waits.push((attempt, delay.as_secs())),
                Next::GiveUp => panic!("gave up too early"),
            }
        }
        assert_eq!(waits, [(1, 1), (2, 2), (3, 4), (4, 8), (5, 16)]);
        assert_eq!(restarts.after_exit(QUICK), Next::GiveUp);
        assert_eq!(restarts.after_start_failure(), Next::GiveUp);
    }

    #[test]
    fn start_failures_count_with_exits_and_a_healthy_run_starts_over() {
        let mut restarts = Restarts::default();
        assert_eq!(restarts.after_exit(QUICK), Next::Retry { attempt: 1, delay: DELAYS[0] });
        assert_eq!(restarts.after_start_failure(), Next::Retry { attempt: 2, delay: DELAYS[1] });
        assert_eq!(restarts.after_exit(HEALTHY), Next::Retry { attempt: 1, delay: DELAYS[0] });
    }

    #[test]
    fn a_user_restart_after_giving_up_starts_over() {
        let mut restarts = Restarts::default();
        while restarts.after_start_failure() != Next::GiveUp {}
        restarts.reset();
        assert_eq!(restarts.after_start_failure(), Next::Retry { attempt: 1, delay: DELAYS[0] });
    }
}
