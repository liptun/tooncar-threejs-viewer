# Instrukcje dla agentów

## Zakres projektu

Repozytorium zawiera dwie powiązane części:

1. aplikację React/Three.js odtwarzającą trasy ToonCar;
2. `export_script/r3d_unpacker.pyw`, który analizuje R3D, generuje assety i przygotowuje sceny Blendera/GLB.

Zmiany w eksporcie mogą wpływać na kontrakt runtime aplikacji (`runtime.json`, skybox, animacje tekstur i GLB). Zawsze sprawdzaj obie strony pipeline’u.

## Obowiązkowa dokumentacja eksportera

Przed zmianą parsera lub eksportera przeczytaj `docs/r3d-format.md` oraz komentarze przy modyfikowanej strukturze w `export_script/r3d_unpacker.pyw`.

Jeżeli zmiana dotyczy któregokolwiek z poniższych obszarów, **w tej samej zmianie obowiązkowo zaktualizuj `docs/r3d-format.md`**:

- offsetu, rozmiaru albo kolejności struktur R3D;
- znaczenia pola lub flagi;
- nowego rodzaju assetu R3D;
- algorytmu wykrywania struktur;
- materiałów, animacji meshów lub animacji tekstur;
- konwersji osi, macierzy albo skali;
- generowanego manifestu lub kontraktu runtime Three.js;
- nowego potwierdzenia uzyskanego z `ToonCar.exe`;
- nowego obszaru jawnie oznaczonego jako nieobsługiwany lub nieznany.

Nie kończ zadania dotyczącego eksportera, jeżeli kod i dokumentacja opisują różne wersje formatu.

## Poziomy pewności

W kodzie i dokumentacji rozróżniaj:

- `EXE` — potwierdzone przez loader/runtime gry;
- `DATA` — potwierdzone empirycznie na assetach;
- `HEURISTIC` — warunek skanera, nie gwarancja formatu;
- `UNKNOWN` — brak wystarczających dowodów.

Nie przedstawiaj heurystyki jako potwierdzonego offsetu. Nie wymyślaj semantyki nieznanych pól tylko dlatego, że wartości wyglądają prawdopodobnie.

## Bezpieczeństwo parsera

- Wszystkie odczyty binarne muszą sprawdzać granice bufora.
- Liczności i stride’y muszą mieć rozsądne limity przed alokacją lub iteracją.
- Floaty muszą być sprawdzane przez `math.isfinite()` tam, gdzie służą do walidacji kandydata.
- Zapisanych wskaźników runtime nie traktuj automatycznie jako offsetów pliku.
- Opcjonalna, nierozpoznana struktura ma zwrócić kontrolowany błąd lub `unsupported_reason`; nie zgaduj jej długości.
- Nie zakładaj wyrównania do 4 bajtów. Znane pliki zawierają niewyrównane banki i meshe.
- Zachowuj informacje diagnostyczne i nieznane pola w manifeście, kiedy jest to możliwe.

## Weryfikacja

Zmianę parsera testuj na więcej niż jednym pliku R3D. Dla tras preferuj zestaw różniący się zawartością, np. Venus, Luna, Vegas i Castilla. Dla samochodu, postaci lub standalone object użyj assetu z właściwej rodziny.

Po zmianach aplikacji uruchom co najmniej:

```bash
pnpm format
pnpm build
```

Przy zmianie kontraktu assetów sprawdź również rzeczywiste załadowanie trasy, animacje meshów, animacje tekstur i skybox.

## Utrzymanie spójności

- `src/tracks.ts` jest źródłem kolejności tras i ich konfiguracji.
- `public/tracks/<id>/runtime.json` jest punktem wejścia do dodatkowych assetów runtime.
- Transformacje meshów odtwarza `THREE.AnimationMixer`.
- Animowane tekstury korzystają z atlasu i `tickFrames` przy 55 Hz.
- Skybox używa sześciu oryginalnych ścian; boczne ściany wymagają orientacji zapisanej w manifeście.
- glTF nie zastępuje manifestów runtime dla Blender World ani sekwencji tekstur.

Jeżeli zmienia się którykolwiek z tych kontraktów, zaktualizuj kod eksportera, loader aplikacji, przykładowe assety oraz dokumentację jako jedną spójną zmianę.
