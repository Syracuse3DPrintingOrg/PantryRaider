# Third-party notices

Pantry Raider ships and builds on software written by other people. This file
lists what travels with the app, what each piece is licensed under, and the
license texts themselves.

Pantry Raider's own code is licensed under PolyForm Noncommercial 1.0 (see
[LICENSE](LICENSE)). Nothing listed here is covered by that license, and
nothing here changes it: each component below stays under its own terms.

## Browser assets served by the app

These files live in the repository and in the container image, and the app
serves them from its own address. A kitchen screen has to work when the
internet is down, so nothing is loaded from a CDN.

| Component | Version | License | Files |
|---|---|---|---|
| [Bootstrap](https://getbootstrap.com) | 5.3.8 | MIT | `service/app/static/vendor/bootstrap.min.css`, `bootstrap.bundle.min.js` |
| [Bootstrap Icons](https://icons.getbootstrap.com) | 1.13.1 | MIT | `service/app/static/vendor/bootstrap-icons.min.css`, `vendor/fonts/` |
| [Bootswatch](https://bootswatch.com) (Cyborg, Darkly, Flatly) | 5.3.3 | MIT | `service/app/static/vendor/themes/cyborg.min.css`, `darkly.min.css`, `flatly.min.css` |
| [html5-qrcode](https://github.com/mebjas/html5-qrcode) | minified browser build | Apache-2.0 | `service/app/static/vendor/html5-qrcode.min.js`, license text in `html5-qrcode.LICENSE` |
| [ESP Web Tools](https://github.com/esphome/esp-web-tools) | 10.3.0 | Apache-2.0 | `service/app/static/js/vendor/esp-web-tools/`, license text in that directory's `LICENSE`, details in its `NOTICE` |

The Bootstrap, Bootstrap Icons and Bootswatch builds carry their own MIT
headers inside the minified files. The two Apache-2.0 bundles ship their
license text as a file beside them, because the upstream minified builds carry
no header of their own.

The Cyborg, Darkly and Flatly themes are Bootswatch themes by Thomas Park.
The other themes in `service/app/static/vendor/themes/` were written for
Pantry Raider and are covered by Pantry Raider's own license.

## Bandit Cub firmware

Bandit Cub firmware is compiled from the ESPHome configurations in `esphome/`.
The build pulls these in; none of them are stored in this repository.

| Component | License | Role |
|---|---|---|
| [ESPHome](https://esphome.io) | dual licensed (GPL-3.0 for the Python tooling, MIT for the generated device code); see the ESPHome repository's `LICENSE` for the authoritative terms | Builds and flashes the firmware |
| [LVGL](https://lvgl.io) | MIT | Draws the Cub screens |
| [ESP-IDF](https://github.com/espressif/esp-idf) and the Espressif managed components it pulls | Apache-2.0 (each ships its own `LICENSE` in the build tree) | ESP32 platform support |

## Python dependencies

Installed with pip from the requirements files, not vendored here. Each
package's own license text ships inside its installed distribution.

### The app (`service/requirements.txt`)

| Package | License |
|---|---|
| fastapi | MIT |
| uvicorn | BSD-3-Clause |
| sqlalchemy | MIT |
| pydantic, pydantic-settings | MIT |
| httpx | BSD-3-Clause |
| python-multipart | Apache-2.0 |
| jinja2 | BSD-3-Clause |
| google-generativeai, google-api-python-client | Apache-2.0 |
| anthropic | MIT |
| Pillow | MIT-CMU (HPND) |
| pypdf | BSD-3-Clause |
| pypdfium2 | BSD-3-Clause and Apache-2.0 (bundles PDFium, BSD-3-Clause) |
| recipe-scrapers | MIT |
| python-dateutil | dual licensed, Apache-2.0 or BSD-3-Clause |
| itsdangerous | BSD-3-Clause |
| pyotp | MIT |
| qrcode | BSD-3-Clause |
| zeroconf | LGPL-2.1-or-later |

### Forager, the cloud companion (`cloud/requirements.txt`)

Everything above that it shares, plus:

| Package | License |
|---|---|
| starlette | BSD-3-Clause |
| alembic | MIT |
| psycopg2-binary | LGPL-3.0-or-later (with the OpenSSL exception) |
| webauthn | BSD-3-Clause |
| cryptography | Apache-2.0 or BSD-3-Clause |

### Kitchen hardware (`streamdeck/requirements.txt`, `gadgets/requirements.txt`)

| Package | License |
|---|---|
| streamdeck (python-elgato-streamdeck) | MIT |
| websockets | BSD-3-Clause |
| bleak | MIT |
| dbus-fast | MIT |
| smbus2 | MIT |
| pyserial | BSD-3-Clause |

The container image also carries a Python base image and the Debian packages it
installs, each under its own license.

## Services run alongside Pantry Raider

Grocy, Mealie, Ollama, CUPS, Watchtower and cloudflared are installed and run
from their own upstream images. Pantry Raider talks to them; it does not
redistribute them, and each stays under its own license. The app's About and
Credits page links to every one of them.

## Data sources

| Source | Terms |
|---|---|
| [Open Food Facts](https://world.openfoodfacts.org) | Database under ODbL; individual product images under CC BY-SA |
| [Open-Meteo](https://open-meteo.com) | Weather data under CC BY 4.0 |
| [TheMealDB](https://www.themealdb.com) | Free API, see their terms |

## License texts

### MIT License

Applies to Bootstrap (Copyright 2011-2025 The Bootstrap Authors), Bootstrap
Icons (Copyright 2019-2024 The Bootstrap Authors), Bootswatch (Copyright
2012-2024 Thomas Park), LVGL (Copyright LVGL Kft), and the MIT-licensed Python
packages listed above, each under its own copyright holder.

```
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

### BSD 3-Clause License

Applies to the BSD-3-Clause packages listed above, each under its own
copyright holder.

```
Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice, this
   list of conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.

3. Neither the name of the copyright holder nor the names of its contributors
   may be used to endorse or promote products derived from this software
   without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```

### MIT-CMU (HPND), as used by Pillow

```
The Python Imaging Library (PIL) is

    Copyright (c) 1997-2011 by Secret Labs AB
    Copyright (c) 1995-2011 by Fredrik Lundh and contributors

Pillow is the friendly PIL fork. It is

    Copyright (c) 2010 by Jeffrey A. Clark and contributors

By obtaining, using, and/or copying this software and/or its associated
documentation, you agree that you have read, understood, and will comply
with the following terms and conditions:

Permission to use, copy, modify and distribute this software and its
documentation for any purpose and without fee is hereby granted,
provided that the above copyright notice appears in all copies, and that
both that copyright notice and this permission notice appear in supporting
documentation, and that the name of Secret Labs AB or the author not be
used in advertising or publicity pertaining to distribution of the software
without specific, written prior permission.

SECRET LABS AB AND THE AUTHOR DISCLAIMS ALL WARRANTIES WITH REGARD TO THIS
SOFTWARE, INCLUDING ALL IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS.
IN NO EVENT SHALL SECRET LABS AB OR THE AUTHOR BE LIABLE FOR ANY SPECIAL,
INDIRECT OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES WHATSOEVER RESULTING FROM
LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION OF CONTRACT, NEGLIGENCE
OR OTHER TORTIOUS ACTION, ARISING OUT OF OR IN CONNECTION WITH THE USE OR
PERFORMANCE OF THIS SOFTWARE.
```

### GNU Lesser General Public License

zeroconf (LGPL-2.1-or-later) and psycopg2 (LGPL-3.0-or-later, with the OpenSSL
exception) are installed unmodified from PyPI and used as libraries. Their full
texts ship with those packages and are published at
<https://www.gnu.org/licenses/lgpl-2.1.html> and
<https://www.gnu.org/licenses/lgpl-3.0.html>.

### Apache License 2.0

Applies to html5-qrcode, ESP Web Tools, ESP-IDF and the Espressif components,
and the Apache-2.0 Python packages listed above, each under its own copyright
holder.

```

                                 Apache License
                           Version 2.0, January 2004
                        http://www.apache.org/licenses/

   TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION

   1. Definitions.

      "License" shall mean the terms and conditions for use, reproduction,
      and distribution as defined by Sections 1 through 9 of this document.

      "Licensor" shall mean the copyright owner or entity authorized by
      the copyright owner that is granting the License.

      "Legal Entity" shall mean the union of the acting entity and all
      other entities that control, are controlled by, or are under common
      control with that entity. For the purposes of this definition,
      "control" means (i) the power, direct or indirect, to cause the
      direction or management of such entity, whether by contract or
      otherwise, or (ii) ownership of fifty percent (50%) or more of the
      outstanding shares, or (iii) beneficial ownership of such entity.

      "You" (or "Your") shall mean an individual or Legal Entity
      exercising permissions granted by this License.

      "Source" form shall mean the preferred form for making modifications,
      including but not limited to software source code, documentation
      source, and configuration files.

      "Object" form shall mean any form resulting from mechanical
      transformation or translation of a Source form, including but
      not limited to compiled object code, generated documentation,
      and conversions to other media types.

      "Work" shall mean the work of authorship, whether in Source or
      Object form, made available under the License, as indicated by a
      copyright notice that is included in or attached to the work
      (an example is provided in the Appendix below).

      "Derivative Works" shall mean any work, whether in Source or Object
      form, that is based on (or derived from) the Work and for which the
      editorial revisions, annotations, elaborations, or other modifications
      represent, as a whole, an original work of authorship. For the purposes
      of this License, Derivative Works shall not include works that remain
      separable from, or merely link (or bind by name) to the interfaces of,
      the Work and Derivative Works thereof.

      "Contribution" shall mean any work of authorship, including
      the original version of the Work and any modifications or additions
      to that Work or Derivative Works thereof, that is intentionally
      submitted to Licensor for inclusion in the Work by the copyright owner
      or by an individual or Legal Entity authorized to submit on behalf of
      the copyright owner. For the purposes of this definition, "submitted"
      means any form of electronic, verbal, or written communication sent
      to the Licensor or its representatives, including but not limited to
      communication on electronic mailing lists, source code control systems,
      and issue tracking systems that are managed by, or on behalf of, the
      Licensor for the purpose of discussing and improving the Work, but
      excluding communication that is conspicuously marked or otherwise
      designated in writing by the copyright owner as "Not a Contribution."

      "Contributor" shall mean Licensor and any individual or Legal Entity
      on behalf of whom a Contribution has been received by Licensor and
      subsequently incorporated within the Work.

   2. Grant of Copyright License. Subject to the terms and conditions of
      this License, each Contributor hereby grants to You a perpetual,
      worldwide, non-exclusive, no-charge, royalty-free, irrevocable
      copyright license to reproduce, prepare Derivative Works of,
      publicly display, publicly perform, sublicense, and distribute the
      Work and such Derivative Works in Source or Object form.

   3. Grant of Patent License. Subject to the terms and conditions of
      this License, each Contributor hereby grants to You a perpetual,
      worldwide, non-exclusive, no-charge, royalty-free, irrevocable
      (except as stated in this section) patent license to make, have made,
      use, offer to sell, sell, import, and otherwise transfer the Work,
      where such license applies only to those patent claims licensable
      by such Contributor that are necessarily infringed by their
      Contribution(s) alone or by combination of their Contribution(s)
      with the Work to which such Contribution(s) was submitted. If You
      institute patent litigation against any entity (including a
      cross-claim or counterclaim in a lawsuit) alleging that the Work
      or a Contribution incorporated within the Work constitutes direct
      or contributory patent infringement, then any patent licenses
      granted to You under this License for that Work shall terminate
      as of the date such litigation is filed.

   4. Redistribution. You may reproduce and distribute copies of the
      Work or Derivative Works thereof in any medium, with or without
      modifications, and in Source or Object form, provided that You
      meet the following conditions:

      (a) You must give any other recipients of the Work or
          Derivative Works a copy of this License; and

      (b) You must cause any modified files to carry prominent notices
          stating that You changed the files; and

      (c) You must retain, in the Source form of any Derivative Works
          that You distribute, all copyright, patent, trademark, and
          attribution notices from the Source form of the Work,
          excluding those notices that do not pertain to any part of
          the Derivative Works; and

      (d) If the Work includes a "NOTICE" text file as part of its
          distribution, then any Derivative Works that You distribute must
          include a readable copy of the attribution notices contained
          within such NOTICE file, excluding those notices that do not
          pertain to any part of the Derivative Works, in at least one
          of the following places: within a NOTICE text file distributed
          as part of the Derivative Works; within the Source form or
          documentation, if provided along with the Derivative Works; or,
          within a display generated by the Derivative Works, if and
          wherever such third-party notices normally appear. The contents
          of the NOTICE file are for informational purposes only and
          do not modify the License. You may add Your own attribution
          notices within Derivative Works that You distribute, alongside
          or as an addendum to the NOTICE text from the Work, provided
          that such additional attribution notices cannot be construed
          as modifying the License.

      You may add Your own copyright statement to Your modifications and
      may provide additional or different license terms and conditions
      for use, reproduction, or distribution of Your modifications, or
      for any such Derivative Works as a whole, provided Your use,
      reproduction, and distribution of the Work otherwise complies with
      the conditions stated in this License.

   5. Submission of Contributions. Unless You explicitly state otherwise,
      any Contribution intentionally submitted for inclusion in the Work
      by You to the Licensor shall be under the terms and conditions of
      this License, without any additional terms or conditions.
      Notwithstanding the above, nothing herein shall supersede or modify
      the terms of any separate license agreement you may have executed
      with Licensor regarding such Contributions.

   6. Trademarks. This License does not grant permission to use the trade
      names, trademarks, service marks, or product names of the Licensor,
      except as required for reasonable and customary use in describing the
      origin of the Work and reproducing the content of the NOTICE file.

   7. Disclaimer of Warranty. Unless required by applicable law or
      agreed to in writing, Licensor provides the Work (and each
      Contributor provides its Contributions) on an "AS IS" BASIS,
      WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
      implied, including, without limitation, any warranties or conditions
      of TITLE, NON-INFRINGEMENT, MERCHANTABILITY, or FITNESS FOR A
      PARTICULAR PURPOSE. You are solely responsible for determining the
      appropriateness of using or redistributing the Work and assume any
      risks associated with Your exercise of permissions under this License.

   8. Limitation of Liability. In no event and under no legal theory,
      whether in tort (including negligence), contract, or otherwise,
      unless required by applicable law (such as deliberate and grossly
      negligent acts) or agreed to in writing, shall any Contributor be
      liable to You for damages, including any direct, indirect, special,
      incidental, or consequential damages of any character arising as a
      result of this License or out of the use or inability to use the
      Work (including but not limited to damages for loss of goodwill,
      work stoppage, computer failure or malfunction, or any and all
      other commercial damages or losses), even if such Contributor
      has been advised of the possibility of such damages.

   9. Accepting Warranty or Additional Liability. While redistributing
      the Work or Derivative Works thereof, You may choose to offer,
      and charge a fee for, acceptance of support, warranty, indemnity,
      or other liability obligations and/or rights consistent with this
      License. However, in accepting such obligations, You may act only
      on Your own behalf and on Your sole responsibility, not on behalf
      of any other Contributor, and only if You agree to indemnify,
      defend, and hold each Contributor harmless for any liability
      incurred by, or claims asserted against, such Contributor by reason
      of your accepting any such warranty or additional liability.

   END OF TERMS AND CONDITIONS

   APPENDIX: How to apply the Apache License to your work.

      To apply the Apache License to your work, attach the following
      boilerplate notice, with the fields enclosed by brackets "[]"
      replaced with your own identifying information. (Don't include
      the brackets!)  The text should be enclosed in the appropriate
      comment syntax for the file format. We also recommend that a
      file or class name and description of purpose be included on the
      same "printed page" as the copyright notice for easier
      identification within third-party archives.

   Copyright [yyyy] [name of copyright owner]

   Licensed under the Apache License, Version 2.0 (the "License");
   you may not use this file except in compliance with the License.
   You may obtain a copy of the License at

       http://www.apache.org/licenses/LICENSE-2.0

   Unless required by applicable law or agreed to in writing, software
   distributed under the License is distributed on an "AS IS" BASIS,
   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
   See the License for the specific language governing permissions and
   limitations under the License.
```
