# kf command quick reference
kf --json capture url "<url>" [--note "..."]
kf --json capture images <img1> <img2> ...
kf --json capture video "<url>"
kf --json capture file "<path>"
kf --json capture retry <src_id>
kf --json status [<ref>]
kf --json search "<query>" [-n 10]
kf --json taxonomy garden --dry-run
kf --json taxonomy apply <proposal-id> --approved-by liang
kf --json taxonomy list
kf --json maps rebuild [--scope <category-id>]
kf --json index rebuild
kf --json brief build --daily|--weekly [--date YYYY-MM-DD]
kf --json podcast build --daily|--weekly [--date YYYY-MM-DD]
kf --json skill diff|install|verify
kf --json backup create
kf --json backup verify <backup-id>
kf --json restore test <backup-id>
kf --json migrate plan|apply|verify|rollback [--live --approved-by liang]
kf --json doctor [--fix-safe] [--stamp]
