from journalcheck.bootstrap import bootstrap_vendor
from journalcheck.cli import main


bootstrap_vendor()

if __name__ == "__main__":
    raise SystemExit(main())
