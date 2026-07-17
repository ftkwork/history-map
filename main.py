"""历史版图 — 桌面应用入口。"""



import sys



from PySide6.QtCore import Qt

from PySide6.QtWidgets import QApplication, QMessageBox



from app.data import PREPARE_DATA_HINT, DataNotPreparedError, SnapshotStore, is_app_data_ready

from app.log import reset_log, trace

from app.ui import MainWindow





def main() -> int:

    QApplication.setHighDpiScaleFactorRoundingPolicy(

        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough

    )

    app = QApplication(sys.argv)

    app.setApplicationName("历史版图")

    app.setOrganizationName("历史版图")



    if not is_app_data_ready():

        QMessageBox.critical(None, "数据未准备", PREPARE_DATA_HINT)

        return 1



    reset_log()

    trace("APP", "starting")



    try:

        store = SnapshotStore()

    except DataNotPreparedError as exc:

        QMessageBox.critical(None, "数据未准备", str(exc))

        return 1



    window = MainWindow(store)

    window.show()

    return app.exec()





if __name__ == "__main__":

    sys.exit(main())

