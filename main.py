"""拾光统一入口：桌面应用，或使用 --wxdump-cli 进入数据库命令行。"""
import sys


def main():
    if len(sys.argv) > 1 and sys.argv[1] == '--wxdump-cli':
        sys.argv.pop(1)
        from wxdump import main as cli_main
        return cli_main()
    from wxdesk.desktop import main as desktop_main
    return desktop_main()


if __name__ == '__main__':
    raise SystemExit(main())
