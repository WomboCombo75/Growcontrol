<?php
header('Content-Type: application/json');
$namesFile = 'names.json';
if ($_SERVER['REQUEST_METHOD'] === 'POST') {
    $data = json_decode(file_get_contents('php://input'), true);
    if ($data && isset($data['key']) && isset($data['name'])) {
        // Bestehende Namen laden (falls vorhanden)
        if (file_exists($namesFile)) {
            $names = json_decode(file_get_contents($namesFile), true);
        } else {
            $names = array();
        }
        // Den Namen aktualisieren
        $names[$data['key']] = $data['name'];
        // Datei speichern
        if(file_put_contents($namesFile, json_encode($names, JSON_PRETTY_PRINT))){
            echo json_encode(array("success" => true));
            exit;
        }
    }
}
echo json_encode(array("success" => false));
?>
