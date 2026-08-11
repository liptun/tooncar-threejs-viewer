# ToonCar R3D — stan reverse engineeringu

> Dokument roboczy dla agentów AI rozwijających `export_script/r3d_unpacker.pyw`.
> Opisuje stan wiedzy wynikający z analizy plików gry i loaderów w `ToonCar.exe`.
> Nie jest kompletną specyfikacją formatu R3D.

Stan dokumentu odpowiada eksporterowi **v102**.

## 1. Jak czytać ten dokument

Każda informacja należy do jednej z czterech kategorii:

- **EXE** — kolejność lub znaczenie potwierdzone w kodzie `ToonCar.exe`;
- **DATA** — zachowanie potwierdzone na znanych assetach, ale nie w pełni nazwane przez kod gry;
- **HEURISTIC** — warunek używany do odnajdywania struktur bez znanego nadrzędnego grafu serializacji;
- **UNKNOWN** — pole albo podstruktura, której semantyka nadal nie jest znana.

Nie zamieniaj informacji `HEURISTIC` lub `UNKNOWN` w „potwierdzony format” bez nowych dowodów. Adresy funkcji odnoszą się do analizowanej wersji `ToonCar.exe` i mogą nie pasować do innego wydania gry.

## 2. Najkrótszy model mentalny

R3D nie jest jednym płaskim formatem o identycznym nagłówku dla każdego assetu. To rodzina binarnych serializacji używających wspólnych struktur:

- banków tekstur;
- tabel materiałów i animacji tekstur;
- dwóch różnych reprezentacji mesha (`StaticMesh` i `ObjectMesh`);
- hierarchii `Root` / `Part` dla obiektów mapy;
- macierzy instancji;
- opcjonalnych struktur kolizji, animacji, szkieletu i morphingu;
- danych gameplayu trasy.

Pliki są little-endian. Wiele struktur zawiera zapisane wartości pól będących wskaźnikami w runtime. W pliku taka wartość często oznacza jedynie „opcjonalny blok istnieje”; nie należy automatycznie interpretować jej jako offsetu w pliku.

Aktualny eksporter łączy dwa podejścia:

1. sekwencyjne parsowanie potwierdzonego początku pliku trasy;
2. skanowanie całego pliku silnymi sygnaturami struktur, których pełny graf nadrzędny nie został jeszcze odtworzony.

## 3. Potwierdzony początek pliku trasy

`parse_code_guided_top_level()` wymaga następującej kolejności od offsetu `0`:

```text
TextureBank
uint32 material_count
Material[material_count]       # rekord 0x60
uint32 texture_animation_count
TextureAnimation[count]        # rekord 0x1A8
StaticMesh                      # nagłówek 0x40 + bufory
... dalszy graf trasy, częściowo rozpoznany ...
```

Jest to najmocniej potwierdzony fragment formatu tras. Jeśli plik nie zaczyna się prawidłowym bankiem tekstur albo bezpośrednio po tabelach nie ma poprawnego `StaticMesh`, skrypt nie powinien udawać, że rozpoznał plik trasy.

## 4. Bank tekstur

Źródło: loader `0x4541A0` (**EXE**) oraz znane pliki (**DATA**).

```text
uint32 texture_count
repeat texture_count:
    char name[0x80]             # C-string, zwykle BMP/TGA/DDS/PNG/JPG
    uint32 width                # +0x80
    uint32 height               # +0x84
    uint32 flags                # +0x88
    uint32 unknown              # +0x8C
    uint32 descriptor_size      # +0x90; znane wartości 0 lub 0x14
    byte bgra[width*height*4]   # surowe piksele
```

Stała `TEXTURE_HEADER_SIZE` wynosi `148` (`0x94`).

Potwierdzone zachowanie:

- piksele są zapisane jako BGRA i przy eksporcie muszą zostać zamienione na RGBA;
- `flags & 0x80000000` oznacza, że kanał alfa ma być zachowany;
- bez flagi alfa eksporter wymusza `A=255`;
- tryb `opaque` / `clip` / `blend` jest klasyfikacją eksportera opartą na pikselach, nie polem R3D;
- bank skyboxa jest rozpoznawany po komplecie nazw kończących się `UP`, `DN`, `FR`, `BK`, `LF`, `RT` (**DATA/HEURISTIC**).

`find_all_texture_banks()` szuka kandydatów na podstawie nazw obrazów, a nie wyrównania do 4 bajtów. Jest to celowe: co najmniej Luna zawiera bank, który nie jest wyrównany do DWORD.

Nieznane:

- pełne znaczenie `flags` poza bitem alfa;
- znaczenie DWORD `unknown`;
- semantyka `descriptor_size` poza obserwowanymi wartościami.

## 5. Materiały

Źródło: główny loader trasy (**EXE**) oraz hash zasobów (**EXE/DATA**).

Każdy rekord ma `0x60` bajtów (`24 × uint32`, interpretowanych diagnostycznie również jako floaty).

| Offset | Typ | Znaczenie | Pewność |
| --- | --- | --- | --- |
| `+0x00` | `uint32` | identyfikator zasobu materiału | EXE/DATA |
| `+0x38` | `uint32` | hash nazwy pliku tekstury | EXE/DATA |
| pozostałe |  | parametry materiału, w większości nienazwane | UNKNOWN |

Hash z `+0x38` jest wyliczany przez algorytm z `ToonCar.exe` `0x47C6E0`. Jest case-insensitive w rozumieniu gry. `tooncar_filename_hash()` jest źródłem prawdy dla mapowania materiał → tekstura. Nie zastępuj go dopasowaniem po indeksie ani nazwie „na oko”.

Znane wektory kontrolne:

```text
piedras25.bmp       -> 0xF15A
Bandera_Tooncar.bmp -> 0x18D93
```

Kolizje hashy i brakujące dopasowania muszą być raportowane w manifeście, a nie cicho rozwiązywane losową teksturą.

## 6. Animacje tekstur

Źródło: tabela z głównego loadera, runtime `0x4027F0` i stałe czasu gry (**EXE**).

Rekord ma `0x1A8` bajtów (`106 × uint32`).

| Offset | Typ | Znaczenie | Pewność |
| --- | --- | --- | --- |
| `+0x00` | `uint32` | resource ID animowanego materiału | EXE/DATA |
| `+0x04` | `uint32` | nierozpoznane | UNKNOWN |
| `+0x08` | `uint32` | liczba klatek | EXE/DATA |
| `+0x0C` | `uint32[]` | resource ID kolejnych klatek | EXE/DATA |
| `+0x1A4` | `float` | krok fazy na tick | EXE |

Ważne: `+0x1A4` **nie oznacza sekund na klatkę**. Gra aktualizuje animacje z częstotliwością `55 Hz` (`1/55 s`) i odejmuje krok fazy w każdym ticku. `build_tooncar_texture_tick_timeline()` symuluje pełen cykl funkcji `0x4027F0` i generuje `tickFrames`, `cycleTicks` oraz atlas PNG.

Runtime webowy powinien traktować wygenerowane JSON-y jako źródło kolejności klatek. Nie należy próbować odtwarzać atlasu przez przesuwanie globalnego UV materiału — pojedyncza klatka jest kopiowana na osobny canvas, aby wrapping tekstury powtarzał tylko bieżącą klatkę, a nie sąsiednie komórki atlasu.

## 7. StaticMesh (`0x460880`)

Loader (**EXE**):

```text
header[0x40]
vertices[vertex_count]          # stride 0x30
faces[face_count]               # stride 0x24
```

Znane pola nagłówka:

| Offset | Typ | Znaczenie | Pewność |
| --- | --- | --- | --- |
| `+0x00` | `uint32` | vertex count | EXE/DATA |
| `+0x08` | `uint32` | zero w znanych zapisach; silna sygnatura | DATA/HEURISTIC |
| `+0x0C` | `uint32` | zero w znanych zapisach; silna sygnatura | DATA/HEURISTIC |
| `+0x10` | `uint32` | face count | EXE/DATA |
| `+0x18` | `uint32` | liczba slotów materiałów | DATA |
| `+0x20` | `float[6]` | bounding box | DATA |

Vertex `0x30`:

| Offset | Typ | Znaczenie |
| --- | --- | --- |
| `+0x00` | `float3` | pozycja |
| `+0x0C` | `float3` | normalna |
| `+0x18..+0x27` |  | nierozpoznane dane vertexu |
| `+0x28` | `float2` | UV |

Face `0x24`:

| Offset | Typ | Znaczenie |
| --- | --- | --- |
| `+0x00` | `uint16[3]` | indeksy wierzchołków |
| `+0x06` | `uint16` | padding/nieznane |
| `+0x08..+0x1F` |  | nierozpoznane dane face |
| `+0x20` | `uint32` | ID slotu materiału |

`find_all_meshes()` używa ośmiu zer odpowiadających `+0x08..+0x0F` jako sygnatury, a następnie waliduje liczności, zakres indeksów, materiały i skończoność pozycji. To skan heurystyczny; dokładny główny mesh jest dodatkowo potwierdzony przez pozycję w sekwencyjnym prefiksie.

## 8. ObjectMesh (`0x46FAC0`)

`ObjectMesh` nie jest tym samym co `StaticMesh`.

```text
header[0x50]
groups[group_count]             # 0x1C
vertices[vertex_count]          # 0x30
faces[face_count]               # 0x06
extra[extra_count]              # 0x08; semantyka nieznana
```

Pola nagłówka:

| Offset | Znaczenie |
| --- | --- |
| `+0x14` | group count |
| `+0x18` | vertex count |
| `+0x24` | face count |
| `+0x2C` | extra count |

Grupa `0x1C`:

```text
+0x00 uint32 global_material_id
+0x04 uint32 vertex_count
+0x08 uint32 vertex_start
+0x0C uint32 face_count
+0x10 uint32 face_start
+0x14 uint32 unknown
+0x18 uint32 unknown
```

Face ma trzy `uint16`, a indeksy są lokalne względem zakresu vertexów grupy. Znane meshe dzielą tablice vertexów i face’ów dokładnie pomiędzy grupy; parser wykorzystuje to jako walidację.

Skan odbywa się bajt po bajcie, ponieważ struktury nie muszą być wyrównane do DWORD.

## 9. Obiekty mapy: Root, Part, instancje

Źródła: loadery `0x449860`, `0x449810`, `0x46FAC0`, `0x45B530` (**EXE**).

### Root (`0x50`)

- `+0x18` i `+0x1C` — zapisane wskaźniki opcjonalnych struktur pomocniczych;
- `+0x20` — obecność głównego `Part`;
- `+0x24` — liczba child parts;
- sloty od `+0x28` — obecność kolejnych child parts.

Nie traktuj wartości wskaźnikowych jako offsetów. Loader czyta kolejne bloki sekwencyjnie, jeżeli zapisane pole jest niezerowe.

### Part (`0x30`)

- `+0x18` — obecność `ObjectMesh`;
- `+0x1C` — obecność bloku spatial/collision;
- `+0x20` — obecność `StaticMesh`;
- `+0x24`, `+0x28`, `+0x2C` — lokalna translacja `float3` (**DATA**, wymagana m.in. dla złożonych obiektów Wenus).

### Spatial/collision (`0x45B530`)

```text
header[0x68]
records[count]                  # 0x5C
```

- count jest w `+0x30`;
- `+0x38` sygnalizuje opcjonalny zagnieżdżony obiekt;
- semantyka rekordów `0x5C` i długość zagnieżdżonego obiektu nie są jeszcze rozpoznane.

Jeśli opcjonalny nested pointer jest ustawiony, parser ma zgłosić brak obsługi zamiast zgadywać długość.

### Tabela instancji

Po definicjach Root:

```text
uint32 instance_count
Instance[instance_count]        # 0x44
```

Instance:

- `+0x00` — `float[16]`, macierz Direct3D w konwencji row-vector;
- `+0x40` — indeks definicji Root/modelu.

Na Wenus model ten rozwiązuje się do 4 definicji Root i 20 instancji (**DATA**).

Parser ma wariant ścisły i elastyczny. Elastyczny rekonstruuje granice za pomocą poprawnych Root/Part/ObjectMesh oraz dokładnej tabeli instancji, ale świadomie nie dekoduje nieznanych payloadów `Root +0x18/+0x1C`.

## 10. Animowane obiekty mapy

Znane stałe:

```text
ANIM_NODE_SERIALIZED_SIZE = 0x110
ANIM_KEY_SIZE             = 0x28
ANIM_SET_HEADER_SIZE      = 0x24
map phase step            = 1/6
updates per key interval  = 6
loop mode                 = forward loop
```

Powiązane funkcje EXE:

- animated model load `0x452840`;
- ObjectMesh list load `0x471920`;
- node load `0x4751A0`;
- animation set load `0x4734E0`;
- track sampler `0x473020`;
- map setup `0x401993 → 0x474620`.

Model animowany zawiera sekwencyjnie:

1. liczbę i listę `ObjectMesh`;
2. mapowanie node → mesh (`0xFFFFFFFF` oznacza brak);
3. listę przyczepionych wektorów;
4. marker opcjonalnego morpha;
5. rekurencyjne drzewo node’ów `0x110`;
6. opcjonalny animation set.

Node `0x110` — znane pola:

- `+0x04` node index;
- `+0x28` zapisany wskaźnik mesha;
- `+0x2C` marker opcjonalnego payloadu `0x4C`;
- `+0x30` macierz lokalna `float[16]`;
- `+0x70` druga macierz, używana jako global/bind w pokrewnych assetach;
- `+0xF0` child count;
- `+0xF8` flags.

Animation key `0x28`:

```text
+0x00 float4 quaternion XYZW
+0x10 float3 translation
+0x1C float3 scale
```

Animation set zawiera nagłówek `0x24`, mapowanie node → track oraz dla każdego tracka `uint32 key_count` i `key_count × 0x28`.

Nieobsługiwane:

- morph/deformation payload `0x47BAA0` — marker jest wykrywany, ale jego długość i semantyka nie są zgadywane;
- część opcjonalnych struktur node/root niewystępujących na dotychczas testowanych animacjach Luny i Kastylii.

## 11. Dane gameplayu trasy

Potwierdzone komendy skryptowe gry (**EXE**):

- `Way` — lista punktów trasy AI;
- `Lap` — dane okrążenia;
- `Conos` — punkty pachołków;
- `Sorpresa` — pozycje skrzynek/pickupów.

Serializator list `Vec3`: `0x475C50`, loader `0x475C80`. Punkt to `3 × float` (`0x0C`). Runtime tworzy skrzynki przez `0x414880`.

Loader trasy `0x447630` zapisuje także:

- stały blok `0x280`;
- tabele rekordów o stride `0x1C` i `0xB4`;
- dalsze dane, których pełna semantyka nie jest jeszcze nazwana.

`decode_gameplay_track_data()` korzysta z końcowych granic wcześniej rozpoznanych struktur oraz walidacji liczności. Te sekcje są bardziej zależne od kontekstu niż prefiks trasy.

## 12. Inne rodziny R3D

Detekcja typu assetu znajduje się w `detect_r3d_asset_type()`.

Znane warianty:

- **track** — potwierdzony prefiks opisany wyżej;
- **car** — stały nagłówek `0x23C`, potem bank tekstur, tabela materiałów i meshe; trailer nadal częściowo nieznany;
- **character** — nagłówek `0x1C8`, potem bank tekstur, listy referencji, opcjonalny kontener skinned mesh `0x5C`, drzewo node’ów;
- **rigged object** — bank tekstur, meshe/skin, kontener `0x5C`, drzewo `0x110` i animacje;
- **simple ObjectMesh asset** — pojedyncze wspólne struktury bez tabel materiałów trasy;
- **simple metadata object** — bank tekstur + `ObjectMesh` + nierozpoznany tail `0x64` (np. Mina/Napal);
- **unknown/generic** — tylko bezpieczne skanowanie znanych podstruktur.

Nie zakładaj, że offset banku tekstur z jednego wariantu obowiązuje w innym.

## 13. Układ współrzędnych i eksport

Źródłowe dane są w konwencji Direct3D/ToonCar. Eksporter przy zapisie geometrii do OBJ/Blendera odbija źródłową oś Z, aby skorygować handedness. Skala jest parametrem eksportu (domyślnie `0.1`).

Macierze instancji są zapisane jako row-vector. Konwersję trzeba wykonywać centralnie istniejącymi helperami; nie należy dodawać lokalnych, różniących się wariantów zamiany osi.

Profile materiałów Blendera:

- `game` — wygląd zbliżony do gry;
- `realistic` — podgląd z oświetleniem;
- `gltf` — materiały i timeline przygotowane do eksportu dla Three.js.

Dla presetów Three.js eksporter generuje obok GLB:

```text
runtime.json
texture_animations.json
texture_animations/*.json
texture_animations/*.png
skybox/skybox.json
skybox/{UP,DN,FR,BK,LF,RT}.png
```

glTF nie przenosi Blender World ani sekwencji obrazów w formie wymaganej przez projekt. Dlatego skybox i animacje tekstur są celowo zewnętrznymi assetami runtime.

## 14. Manifest diagnostyczny eksportera

Główny eksport zapisuje `manifest.json`. Zawiera między innymi:

- hash i rozmiar pliku źródłowego;
- wersję eksportera;
- adresy funkcji EXE stanowiących podstawę parsera;
- granice potwierdzonego prefiksu;
- wszystkie znalezione banki tekstur i meshe;
- mapowanie materiałów oraz kolizje hashy;
- placed props i animated props;
- dane gameplayu;
- listę nierozpoznanych surowych fragmentów, jeśli włączono eksport diagnostyczny.

Przy reverse engineeringu porównuj manifesty co najmniej kilku tras. Sukces na jednym pliku nie potwierdza uniwersalności offsetu.

## 15. Co nadal nie jest znane

Najważniejsze otwarte obszary:

- pełny semantyczny graf wszystkich obiektów po głównym meshu trasy;
- znaczenie większości pól materiału `0x60`;
- pozostałe bity flag tekstury i pole `unknown` deskryptora;
- nierozpoznane części vertexów i face’ów `StaticMesh`;
- semantyka `ObjectMesh.extra` (`0x08` na rekord);
- rekordy spatial/collision `0x5C` i ich opcjonalny nested payload;
- struktury pomocnicze Root `+0x18/+0x1C` we wszystkich wariantach;
- payload morphingu `0x47BAA0`;
- pełne znaczenie bloków gameplayu `0x280`, `0x1C`, `0xB4`;
- część nagłówków i trailerów samochodów, postaci i obiektów rigged;
- nazwy i znaczenie wszystkich pól zapisanych jako dawne wskaźniki runtime.

## 16. Zasady bezpiecznego rozwijania parsera

1. Najpierw ustal, czy dowód pochodzi z EXE, danych czy heurystyki.
2. Nie używaj zapisanej wartości wskaźnika jako offsetu bez potwierdzenia.
3. Każdą liczność sprawdzaj względem rozmiaru pliku przed mnożeniem i odczytem.
4. Waliduj `math.isfinite()` dla floatów oraz zakresy indeksów.
5. Nie zakładaj wyrównania struktur; znane banki i ObjectMesh mogą zaczynać się na dowolnym bajcie.
6. Nie maskuj nieobsługiwanej opcjonalnej struktury. Zwróć `unsupported_reason` albo kontrolowany błąd.
7. Zachowuj nieznane pola i surowe dane w manifeście zamiast je zerować.
8. Testuj zmianę na wielu trasach oraz odpowiednim standalone assetcie.
9. Nie zmieniaj jednocześnie parsera, konwersji osi i materiałów bez osobnych dowodów — utrudnia to identyfikację regresji.
10. W tej samej zmianie zaktualizuj ten dokument oraz komentarze `verified_from_exe` w manifeście.

## 17. Punkty wejścia w kodzie

Najważniejsze funkcje w `export_script/r3d_unpacker.pyw`:

| Funkcja | Odpowiedzialność |
| --- | --- |
| `try_texture_bank` | dokładny parser pojedynczego banku tekstur |
| `find_all_texture_banks` | skan kandydatów na banki |
| `parse_material_records` | tabela materiałów `0x60` |
| `parse_animation_records` | tabela animacji tekstur `0x1A8` |
| `try_mesh` / `find_all_meshes` | StaticMesh |
| `try_object_mesh` / `find_all_object_meshes` | ObjectMesh |
| `find_prop_scene_table` | definicje Root/Part i instancje |
| `parse_animated_model_and_tracks` | animowane modele mapy |
| `decode_gameplay_track_data` | Way/Lap/Conos/Sorpresa i tabele trasy |
| `parse_code_guided_top_level` | potwierdzony prefiks pliku trasy |
| `detect_r3d_asset_type` | wybór rodziny parsera |
| `unpack_r3d` | główny eksport trasy i manifest diagnostyczny |
| `build_blend_file` | generowanie sceny Blendera |
| `prepare_gltf_runtime_assets` | atlasy, skybox i manifesty Three.js |
| `run_gui` / `run_cli` | interfejs użytkownika |

Przed większą zmianą wyszukaj również komentarze `Verified from ToonCar.exe` w skrypcie. Część wiedzy o konkretnych funkcjach nadal jest zapisana bezpośrednio przy parserach.
