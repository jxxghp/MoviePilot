import xml.etree.ElementTree as ET
from pathlib import Path


class NfoReader:
    def __init__(self, xml_file_path: Path):
        self.xml_file_path = xml_file_path
        self.tree = ET.parse(xml_file_path)
        self.root = self.tree.getroot()

    def get_element_value(self, element_path) -> str | None:
        element = self.root.find(element_path)
        return element.text if element is not None else None

    def get_elements(self, element_path) -> list[ET.Element]:
        return self.root.findall(element_path)
