"""python -m dailynews 启动开发服务器。"""
import uvicorn


def main() -> None:
    uvicorn.run("dailynews.app:app", host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
