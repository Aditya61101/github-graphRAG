from pathlib import Path

from .client import DriveClient


def main():
    drive = DriveClient()

    files = drive.list_files(
        query="mimeType = 'application/pdf'"
    )

    for file in files:
        print(
            f"{file['name']} | "
            f"{file['id']}"
        )

        content = drive.download_file(file["id"])

        output_path = Path("downloads") / file["name"]
        output_path.parent.mkdir(exist_ok=True)

        output_path.write_bytes(content)

        print(f"Downloaded → {output_path}")


if __name__ == "__main__":
    main()