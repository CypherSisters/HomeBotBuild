# Knowledge packs

HomeBot can read offline knowledge packs: ZIM files from the Kiwix library, read by HomeBot's own reader. This repository does not ship any pack. It lists where each one is downloaded from, how big it is and what its license allows, and you choose which to fetch.

Every pack below is available. The list lives in two files: `config/capability_catalog.json` (one `knowledge_pack` row per pack, with `"available": true`) and `config/source_registry.json` (one download row per file).

## How downloads are named

Kiwix puts a date in each file name (`wikipedia_en_all_nopic_YYYY-MM.zim`) and replaces the file on its own schedule. The download address uses the undated name, `https://download.kiwix.org/zim/<name>.zim`, which redirects to the newest dated file. Where a row lists a known-good file, that dated version is known to open in HomeBot's reader; a newer one is expected to work the same way.

Sizes are approximate and are those of the newest file. The wizard shows the real size before a download starts.

## Licenses

- **Commercial use: no** means the license is non-commercial (NC), allows no derivatives (ND), or has not been confirmed. These packs are fine for personal use at home. Do not resell them or bundle them into a product.
- **Checked: no** means the license could not be confirmed from the publisher. The best-known terms are shown; read the license page before relying on them.
- The Survivor Library is mostly scans of old books that the publisher describes as public domain. Rights are mixed and have not been checked title by title.
- Medical packs are reference material, not medical advice.

## Encyclopedias and general reference

| Pack | Download name | Size | License | Commercial use | Checked |
|---|---|---|---|---|---|
| Wikipedia, offline: All of English Wikipedia without images. Enormous, and the broadest single thing on this list. | `wikipedia_en_all_nopic` | 49.1 GB | [CC-BY-SA-4.0](https://en.wikipedia.org/wiki/Wikipedia:Copyrights) | yes | yes |
| Wikipedia (English, with images): All of English Wikipedia with images. The largest single pack on this list. | `wikipedia_en_all_maxi` | 118.7 GB | [CC-BY-SA-4.0](https://foundation.wikimedia.org/wiki/Policy:Terms_of_Use) | yes | yes |
| Wikipedia (English, article summaries): The opening section of every English Wikipedia article, without images. | `wikipedia_en_all_mini` | 13.4 GB | [CC-BY-SA-4.0](https://foundation.wikimedia.org/wiki/Policy:Terms_of_Use) | yes | yes |
| Best of Wikipedia (English): A selection of about 50,000 of the best English Wikipedia articles, with images. | `wikipedia_en_top_maxi` | 6.5 GB | [CC-BY-SA-4.0](https://foundation.wikimedia.org/wiki/Policy:Terms_of_Use) | yes | yes |
| Wikipedia history articles: Wikipedia's history articles as a standalone pack, without images. | `wikipedia_en_history_nopic` | 701 MB | [CC-BY-SA-4.0](https://foundation.wikimedia.org/wiki/Policy:Terms_of_Use) | yes | yes |
| Wiktionary dictionary: The English Wiktionary: definitions, spelling, pronunciation and word origins, without images. | `wiktionary_en_all_nopic` | 8.5 GB | [CC-BY-SA-4.0](https://foundation.wikimedia.org/wiki/Policy:Terms_of_Use) | yes | yes |
| Explain xkcd: Every xkcd comic with a community explanation of the joke. The comics themselves are non-commercial only. | `explainxkcd_en_all_maxi` | 147 MB | [CC-BY-SA-3.0 (comic images CC-BY-NC-2.5)](https://www.explainxkcd.com/wiki/index.php/explain_xkcd:Copyrights) | no | yes |
| RationalWiki: Articles analysing and refuting pseudoscience and anti-science claims, with images. | `rationalwiki_en_all_maxi` | 238 MB | [CC-BY-SA-3.0](https://rationalwiki.org/wiki/RationalWiki:Copyrights) | no | no |
| RationalWiki (text only): Articles analysing and refuting pseudoscience and anti-science claims, without images. | `rationalwiki_en_all_nopic` | 80 MB | [CC-BY-SA-3.0](https://rationalwiki.org/wiki/RationalWiki:Copyrights) | no | no |

## Learning and textbooks

| Pack | Download name | Size | License | Commercial use | Checked |
|---|---|---|---|---|---|
| Simple English Wikipedia: Wikipedia written in simple English words and grammar, with images. | `wikipedia_en-simple_all_maxi` | 2.9 GB | [CC-BY-SA-4.0](https://foundation.wikimedia.org/wiki/Policy:Terms_of_Use) | yes | yes |
| Wikibooks open textbooks: Open textbooks and manuals on many subjects, without images. | `wikibooks_en_all_nopic` | 3.3 GB | [CC-BY-SA-4.0](https://foundation.wikimedia.org/wiki/Policy:Terms_of_Use) | yes | yes |
| Wikiversity courses: Open course material and learning projects, without images. | `wikiversity_en_all_nopic` | 1.5 GB | [CC-BY-SA-4.0](https://foundation.wikimedia.org/wiki/Policy:Terms_of_Use) | yes | yes |

## Science and nature

| Pack | Download name | Size | License | Commercial use | Checked |
|---|---|---|---|---|---|
| Wikispecies: A catalogue of all known species and their classification, with images. | `wikispecies_en_all_maxi` | 3.2 GB | [CC-BY-SA-4.0](https://foundation.wikimedia.org/wiki/Policy:Terms_of_Use) | yes | yes |
| Science and astronomy Q&A (Stack Exchange): Every question and answer from the Astronomy Stack Exchange, offline. Every question and answer from the Space Exploration Stack Exchange, offline. | `astronomy.stackexchange.com_en_all`, `space.stackexchange.com_en_all` | 566 MB | [CC-BY-SA-4.0](https://stackoverflow.com/help/licensing) | yes | yes |
| Wikipedia physics articles: Wikipedia's physics articles as a standalone pack, with images. | `wikipedia_en_physics_maxi` | 1.2 GB | [CC-BY-SA-4.0](https://foundation.wikimedia.org/wiki/Policy:Terms_of_Use) | yes | yes |
| Wikispecies (text only): A catalogue of all known species and their classification, without images. | `wikispecies_en_all_nopic` | 1.4 GB | [CC-BY-SA-4.0](https://foundation.wikimedia.org/wiki/Policy:Terms_of_Use) | yes | yes |
| Physics Q&A (Stack Exchange): Every question and answer from the Physics Stack Exchange, offline. | `physics.stackexchange.com_en_all` | 1.7 GB | [CC-BY-SA-4.0](https://stackoverflow.com/help/licensing) | yes | yes |
| Gardening and Landscaping Q&A (Stack Exchange): Every question and answer from the Gardening and Landscaping Stack Exchange, offline. | `gardening.stackexchange.com_en_all` | 884 MB | [CC-BY-SA-4.0](https://stackoverflow.com/help/licensing) | yes | yes |

## Travel and maps

| Pack | Download name | Size | License | Commercial use | Checked |
|---|---|---|---|---|---|
| Wikivoyage travel guide: The Wikivoyage travel guide, with images. | `wikivoyage_en_all_maxi` | 1022 MB | [CC-BY-SA-4.0](https://foundation.wikimedia.org/wiki/Policy:Terms_of_Use) | yes | yes |
| OpenStreetMap wiki: The OpenStreetMap wiki: how the map is made, tagged and used, with images. | `openstreetmap-wiki_en_all_maxi` | 1.2 GB | [CC-BY-SA-2.0](https://wiki.openstreetmap.org/wiki/Wiki_content_license) | yes | yes |
| Wikivoyage travel guide (text only): The Wikivoyage travel guide without images. Much smaller than the full version. | `wikivoyage_en_all_nopic` | 260 MB | [CC-BY-SA-4.0](https://foundation.wikimedia.org/wiki/Policy:Terms_of_Use) | yes | yes |
| OpenStreetMap wiki (text only): The OpenStreetMap wiki: how the map is made, tagged and used, without images. | `openstreetmap-wiki_en_all_nopic` | 456 MB | [CC-BY-SA-2.0](https://wiki.openstreetmap.org/wiki/Wiki_content_license) | yes | yes |

## Books and libraries

| Pack | Download name | Size | License | Commercial use | Checked |
|---|---|---|---|---|---|
| Wikisource library of texts: A library of public domain texts: books, poems, speeches and historical documents. | `wikisource_en_all_maxi` | 8.0 GB | [CC-BY-SA-4.0](https://foundation.wikimedia.org/wiki/Policy:Terms_of_Use) | yes | yes |
| Project Gutenberg library: Every English book in Project Gutenberg: classic literature that is public domain in the US. Very large. | `gutenberg_en_all` | 206.1 GB | [PublicDomain-US (Project Gutenberg License)](https://www.gutenberg.org/policy/license.html) | yes | yes |
| Prunelle interactive books: Illustrated interactive books for young readers, from the publisher Book Prunelle. | `prunelle_en_interactive-books` | 359 MB | [Unverified](https://library.kiwix.org/) | no | no |

## Medical reference

| Pack | Download name | Size | License | Commercial use | Checked |
|---|---|---|---|---|---|
| WikEM emergency medicine: WikEM, the open emergency medicine reference, with images. Reference, not advice. | `wikem_en_all_maxi` | 357 MB | [CC-BY-SA-4.0](https://wikem.org/wiki/WikEM:Copyrights) | yes | yes |
| Medicine LibreTexts: Medicine courses and textbooks from LibreTexts. Licenses are set per page; most are non-commercial. | `libretexts.org_en_med` | 1.1 GB | [Mixed: set per page, mostly CC-BY-NC-SA](https://med.libretexts.org/) | no | no |
| WikEM emergency medicine (text only): WikEM, the open emergency medicine reference, without images. Reference, not advice. | `wikem_en_all_nopic` | 331 MB | [CC-BY-SA-4.0](https://wikem.org/wiki/WikEM:Copyrights) | yes | yes |
| MDWiki medical encyclopedia: Healthcare articles curated by WikiProjectMed. Reference, not advice. | `mdwiki_en_all_maxi` | 2.1 GB | [CC-BY-SA-4.0](https://mdwiki.org/wiki/WikiProjectMed:Copyrights) | yes | yes |
| Medical Sciences Q&A (Stack Exchange): Every question and answer from the Medical Sciences Stack Exchange, offline. | `medicalsciences.stackexchange.com_en_all` | 58 MB | [CC-BY-SA-4.0](https://stackoverflow.com/help/licensing) | yes | yes |
| Military medicine manuals: US military medical doctrine and instruction manuals, as collected by the Federation of American Scientists. Reference, not advice. | `irp.fas.org_en_military-medicine` | 72 MB | [PublicDomain-USGov](https://irp.fas.org/) | no | no |
| Field and emergency medicine library: A collection of books on field and emergency medicine. Reference, not advice. | `zimgit-medicine_en` | 67 MB | [Mixed: documents by various authors](https://library.kiwix.org/) | no | no |

## Preparedness and self-reliance

| Pack | Download name | Size | License | Commercial use | Checked |
|---|---|---|---|---|---|
| Water treatment and purification: Resources on collecting and purifying water. | `zimgit-water_en` | 20 MB | [Mixed: documents by various authors](https://library.kiwix.org/) | no | no |
| Knots library: Illustrated resources on all kinds of knots, lashings and ladders. | `zimgit-knots_en` | 27 MB | [Mixed: documents by various authors](https://library.kiwix.org/) | no | no |
| Ready.gov emergency prep: The US government's disaster preparedness site: weather, fire, making a plan and building a kit. | `www.ready.gov_en` | 2.3 GB | [PublicDomain-USGov](https://www.ready.gov/website-policies) | no | no |
| Survivor Library: Scanned books and manuals, mostly 1800s to early 1900s, on making and growing things without modern infrastructure. Rights are mixed: mostly old public domain works, not checked title by title. Very large. | `survivorlibrary.com_en_all` | 205.2 GB | [Mixed: mostly public domain scans, rights not checked title by title](https://www.survivorlibrary.com/) | no | no |
| Great Outdoors Q&A (Stack Exchange): Every question and answer from the Great Outdoors Stack Exchange, offline. | `outdoors.stackexchange.com_en_all` | 135 MB | [CC-BY-SA-4.0](https://stackoverflow.com/help/licensing) | yes | yes |
| Ready.gov emergency prep (Spanish): The Spanish edition of the US government's disaster preparedness site. | `www.ready.gov_es` | 2.4 GB | [PublicDomain-USGov](https://www.ready.gov/website-policies) | no | no |
| Urban Prepper videos: Videos from the Urban Prepper channel on emergency preparedness in cities. | `urban-prepper_en_all` | 2.1 GB | [Unverified: videos from a YouTube channel](https://library.kiwix.org/) | no | no |
| Canadian Prepper: winter prepping videos: Videos from the Canadian Prepper channel on preparing for winter storms and cold weather emergencies. | `canadian-prepper_en_winterprepping` | 1.2 GB | [Unverified: videos from a YouTube channel](https://library.kiwix.org/) | no | no |
| Food for Preppers: A collection of recipes and techniques for preparing and storing food. | `zimgit-food-preparation_en` | 93 MB | [Mixed: documents by various authors](https://library.kiwix.org/) | no | no |
| Post-disaster resource library: A collection of guides for the weeks after a disaster: shelter, sanitation, repair and recovery. | `zimgit-post-disaster_en` | 615 MB | [Mixed: documents by various authors](https://library.kiwix.org/) | no | no |

## Repair, home and food

| Pack | Download name | Size | License | Commercial use | Checked |
|---|---|---|---|---|---|
| iFixit repair guides: Step-by-step teardown and repair guides for consumer hardware. Non-commercial use only. | `ifixit_en_all` | 3.3 GB | [CC-BY-NC-SA-3.0](https://www.ifixit.com/Info/Licensing) | no | yes |
| Home Improvement Q&A (Stack Exchange): Every question and answer from the Home Improvement Stack Exchange, offline. | `diy.stackexchange.com_en_all` | 1.9 GB | [CC-BY-SA-4.0](https://stackoverflow.com/help/licensing) | yes | yes |
| Motor Vehicle Maintenance and Repair Q&A (Stack Exchange): Every question and answer from the Motor Vehicle Maintenance and Repair Stack Exchange, offline. | `mechanics.stackexchange.com_en_all` | 324 MB | [CC-BY-SA-4.0](https://stackoverflow.com/help/licensing) | yes | yes |
| Cooking Q&A (Stack Exchange): Every question and answer from the Cooking Stack Exchange (Seasoned Advice), offline. | `cooking.stackexchange.com_en_all` | 226 MB | [CC-BY-SA-4.0](https://stackoverflow.com/help/licensing) | yes | yes |
| Homebrewing Q&A (Stack Exchange): Every question and answer from the Homebrewing Stack Exchange, offline. | `homebrew.stackexchange.com_en_all` | 36 MB | [CC-BY-SA-4.0](https://stackoverflow.com/help/licensing) | yes | yes |
| Sustainable Living Q&A (Stack Exchange): Every question and answer from the Sustainable Living Stack Exchange, offline. | `sustainability.stackexchange.com_en_all` | 26 MB | [CC-BY-SA-4.0](https://stackoverflow.com/help/licensing) | yes | yes |

## Technology

| Pack | Download name | Size | License | Commercial use | Checked |
|---|---|---|---|---|---|
| MediaWiki documentation: The manual and help pages for MediaWiki, the software that runs Wikipedia. | `mediawiki_en_all` | 6.4 GB | [CC-BY-SA-4.0](https://www.mediawiki.org/wiki/Project:Copyrights) | yes | yes |
| Incognito Cat digital privacy guide: Plain guides to tools and habits that protect your privacy online. | `incognitocat.me_en_all` | 9 MB | [CC-BY-SA-4.0](https://incognitocat.me/) | yes | yes |
| Electrical Engineering Q&A (Stack Exchange): Every question and answer from the Electrical Engineering Stack Exchange, offline. | `electronics.stackexchange.com_en_all` | 3.9 GB | [CC-BY-SA-4.0](https://stackoverflow.com/help/licensing) | yes | yes |
| Software Engineering Q&A (Stack Exchange): Every question and answer from the Software Engineering Stack Exchange, offline. | `softwareengineering.stackexchange.com_en_all` | 457 MB | [CC-BY-SA-4.0](https://stackoverflow.com/help/licensing) | yes | yes |
| Information Security Q&A (Stack Exchange): Every question and answer from the Information Security Stack Exchange, offline. | `security.stackexchange.com_en_all` | 419 MB | [CC-BY-SA-4.0](https://stackoverflow.com/help/licensing) | yes | yes |
| Engineering Q&A (Stack Exchange): Every question and answer from the Engineering Stack Exchange, offline. | `engineering.stackexchange.com_en_all` | 244 MB | [CC-BY-SA-4.0](https://stackoverflow.com/help/licensing) | yes | yes |
| Amateur Radio Q&A (Stack Exchange): Every question and answer from the Amateur Radio Stack Exchange, offline. | `ham.stackexchange.com_en_all` | 72 MB | [CC-BY-SA-4.0](https://stackoverflow.com/help/licensing) | yes | yes |

## TED talks

| Pack | Download name | Size | License | Commercial use | Checked |
|---|---|---|---|---|---|
| TED talks: coronavirus: TED talk videos about the coronavirus pandemic, with subtitles in many languages. Non-commercial use only, no edited versions. | `ted_mul_coronavirus` | 1.3 GB | [CC-BY-NC-ND-4.0](https://www.ted.com/about/our-organization/our-policies-terms/ted-talks-usage-policy) | no | yes |
| TED talks: democracy: TED talk videos about democracy, with subtitles in many languages. Non-commercial use only, no edited versions. | `ted_mul_democracy` | 2.2 GB | [CC-BY-NC-ND-4.0](https://www.ted.com/about/our-organization/our-policies-terms/ted-talks-usage-policy) | no | yes |
| TED talks: human rights: TED talk videos about human rights, with subtitles in many languages. Non-commercial use only, no edited versions. | `ted_mul_human-rights` | 558 MB | [CC-BY-NC-ND-4.0](https://www.ted.com/about/our-organization/our-policies-terms/ted-talks-usage-policy) | no | yes |
| TED talks: justice system: TED talk videos about the justice system, with subtitles in many languages. Non-commercial use only, no edited versions. | `ted_mul_justice-system` | 966 MB | [CC-BY-NC-ND-4.0](https://www.ted.com/about/our-organization/our-policies-terms/ted-talks-usage-policy) | no | yes |
| TED talks: law: TED talk videos about law, with subtitles in many languages. Non-commercial use only, no edited versions. | `ted_mul_law` | 1.7 GB | [CC-BY-NC-ND-4.0](https://www.ted.com/about/our-organization/our-policies-terms/ted-talks-usage-policy) | no | yes |
| TED talks: maps: TED talk videos about maps and mapping, with subtitles in many languages. Non-commercial use only, no edited versions. | `ted_mul_maps` | 293 MB | [CC-BY-NC-ND-4.0](https://www.ted.com/about/our-organization/our-policies-terms/ted-talks-usage-policy) | no | yes |
| TED talks: military: TED talk videos about the military, with subtitles in many languages. Non-commercial use only, no edited versions. | `ted_mul_military` | 617 MB | [CC-BY-NC-ND-4.0](https://www.ted.com/about/our-organization/our-policies-terms/ted-talks-usage-policy) | no | yes |
| TED talks: plants: TED talk videos about plants, with subtitles in many languages. Non-commercial use only, no edited versions. | `ted_mul_plants` | 633 MB | [CC-BY-NC-ND-4.0](https://www.ted.com/about/our-organization/our-policies-terms/ted-talks-usage-policy) | no | yes |
| TED talks: solar energy: TED talk videos about solar energy, with subtitles in many languages. Non-commercial use only, no edited versions. | `ted_mul_solar-energy` | 515 MB | [CC-BY-NC-ND-4.0](https://www.ted.com/about/our-organization/our-policies-terms/ted-talks-usage-policy) | no | yes |
| TED talks: surveillance: TED talk videos about surveillance, with subtitles in many languages. Non-commercial use only, no edited versions. | `ted_mul_surveillance` | 384 MB | [CC-BY-NC-ND-4.0](https://www.ted.com/about/our-organization/our-policies-terms/ted-talks-usage-policy) | no | yes |
| TED talks: water: TED talk videos about water, with subtitles in many languages. Non-commercial use only, no edited versions. | `ted_mul_water` | 832 MB | [CC-BY-NC-ND-4.0](https://www.ted.com/about/our-organization/our-policies-terms/ted-talks-usage-policy) | no | yes |

## Adding a pack

Add a download row to `config/source_registry.json` (license, license page, undated Kiwix name, approximate size), a `knowledge_pack` row to `config/capability_catalog.json` whose `source_ids` names it, and a line to this page. `tests/test_knowledge_pack_catalog.py` checks that the three agree.
