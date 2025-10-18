import os
import csv
from PyQt6 import QtWidgets, uic
from PyQt6.QtWidgets import QFileDialog, QMessageBox
from dotenv import load_dotenv
from coupang.partners import CoupangPartnersClient, filter_and_score

load_dotenv()

class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        uic.loadUi(os.path.join(os.path.dirname(__file__), 'ui', 'main.ui'), self)
        self.client = CoupangPartnersClient()

        self.searchBtn.clicked.connect(self.search)
        self.deeplinkBtn.clicked.connect(self.make_deeplink)
        self.exportBtn.clicked.connect(self.export_csv)

        self.table.setColumnCount(0)
        self.table.setRowCount(0)
        self._rows = []

    def search(self):
        keyword = self.keywordEdit.text().strip()
        limit = int(self.limitSpin.value())
        sub_id = self.subIdEdit.text().strip() or None

        try:
            raw = self.client.search_products(keyword, limit=limit, sub_id=sub_id)
            data = raw.get('data') or raw
            if isinstance(data, dict):
                for k in ('productData', 'products', 'content', 'items'):
                    if k in data and isinstance(data[k], list):
                        data = data[k]
                        break
            if not isinstance(data, list):
                QMessageBox.warning(self, '오류', '응답 형식이 예상과 다릅니다. 콘솔 로그를 확인하세요.')
                print(raw)
                return

            # filters
            min_price = int(self.minPriceSpin.value())
            min_rating = float(self.minRatingSpin.value())
            min_reviews = int(self.minReviewsSpin.value())
            commission_rate = float(self.commissionSpin.value() / 100.0)

            items = filter_and_score(data, min_price=min_price, min_rating=min_rating, min_reviews=min_reviews, commission_rate=commission_rate)

            self._rows = items
            self.populate_table(items)
        except Exception as e:
            QMessageBox.critical(self, '예외', str(e))

    def populate_table(self, items):
        if not items:
            self.table.setColumnCount(0)
            self.table.setRowCount(0)
            return
        headers = list(items[0].keys())
        self.table.setColumnCount(len(headers))
        self.table.setHorizontalHeaderLabels(headers)
        self.table.setRowCount(len(items))
        for r, it in enumerate(items):
            for c, h in enumerate(headers):
                val = it.get(h, '')
                self.table.setItem(r, c, QtWidgets.QTableWidgetItem(str(val)))
        self.table.resizeColumnsToContents()

    def make_deeplink(self):
        if not self._rows:
            QMessageBox.information(self, '알림', '먼저 검색을 실행하세요.')
            return
        # 선택된 행들
        selected = sorted(set(i.row() for i in self.table.selectedIndexes()))
        if not selected:
            QMessageBox.information(self, '알림', '테이블에서 행을 선택하세요.')
            return
        urls = []
        for idx in selected:
            row = self._rows[idx]
            url = row.get('productUrl') or row.get('landingUrl')
            if url and isinstance(url, str) and url.startswith('http'):
                urls.append(url)

        if not urls:
            QMessageBox.warning(self, '경고', 'URL 필드(productUrl/landingUrl)가 없습니다.')
            return

        try:
            resp = self.client.create_deeplinks(urls)
            QMessageBox.information(self, 'Deeplink 결과', str(resp))
        except Exception as e:
            QMessageBox.critical(self, '예외', str(e))

    def export_csv(self):
        if not self._rows:
            QMessageBox.information(self, '알림', '내보낼 데이터가 없습니다.')
            return
        path, _ = QFileDialog.getSaveFileName(self, 'CSV 저장', 'coupang_results.csv', 'CSV Files (*.csv)')
        if not path:
            return
        headers = list(self._rows[0].keys())
        with open(path, 'w', newline='', encoding='utf-8-sig') as f:
            writer = csv.DictWriter(f, fieldnames=headers)
            writer.writeheader()
            for row in self._rows:
                writer.writerow(row)
        QMessageBox.information(self, '완료', '저장되었습니다.')

if __name__ == '__main__':
    app = QtWidgets.QApplication([])
    win = MainWindow()
    win.show()
    app.exec()
