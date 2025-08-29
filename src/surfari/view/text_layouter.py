import re
import surfari.util.config as config
import surfari.util.surfari_logger as surfari_logger
logger = surfari_logger.getLogger(__name__)

def rearrange_texts(input_str, y_threshold=16, h_scale_factor=4, additional_text=""):
    """
    Parses an input string containing lines of the form:
      [frame_name] <text> (x=<val>, y=<val>, w=<val>, h=<val>, xpath=<val>)
    or
      <text> (x=<val>, y=<val>, w=<val>, h=<val>, xpath=<val>, locator_string=<empty_or_not>)

    Groups lines with similar y-coordinates (within y_threshold) into rows,
    sorts each row by x-coordinate, and for each row builds an output line where:

      - Each text item is placed at a column proportional to its x coordinate,
        computed as: target_column = max(0, int(round(x / h_scale_factor))).

      - If multiple items share (y, x), we preserve original input order via _orig_index.

      - Only wraps text at word boundaries if the text length is more than a wrapping factor *
        the "max_col_width," computed from w/h_scale_factor.

      - Excessive internal whitespace is replaced with a single space.

      - Between rows, extra blank lines are inserted if the vertical gap
        exceeds y_threshold, pushing lines downward.
    """

    lines = input_str.strip().splitlines()

    # Regex captures optional frame name, text, x, y, w, h, and xpath
    pattern = re.compile(
        r"^(?:([^\s]+)\s+)?(.*?)\s*\(x=([-\d\.]+),\s*y=([-\d\.]+),\s*w=([\d\.]+),\s*h=([\d\.]+),\s*xpath=(.*?),\s*locator_string=(.*?)\)$"
    )

    entries = []
    for i, line in enumerate(lines):
        line = line.strip()
        if "(x=" not in line or "xpath=" not in line:
            logger.warning(f"Skipping line without expected coordinates/xpath: {line}")
            continue

        match = pattern.match(line)
        if match:
            frame_name = match.group(1) or ""
            raw_text = match.group(2).strip()
            # Collapse all whitespace runs into a single space
            text = re.sub(r"\s+", " ", raw_text)

            x = float(match.group(3))
            y = float(match.group(4))
            w = float(match.group(5))
            h = float(match.group(6))
            xpath = match.group(7).strip()
            
            # allow 1x1 radio, checkbox, or button
            if (w <=1 or h <= 1) and len(text) > 5:
                logger.debug(f"Skipping off-screen or zero-width text: {text}, x={x}, y={y}, w={w}, h={h}")
                continue # Skip off-screen elements          
             
            entries.append(
                {
                    "_orig_index": i,  # preserve input order as a tiebreaker
                    "frame_name": frame_name,
                    "text": text,
                    "x": x,
                    "y": y,
                    "w": w,
                    "h": h,
                    "xpath": xpath,
                }
            )
        else:
            logger.error(f"Line did not match expected pattern: {line}")

    month_pattern = re.compile(
        r"\b("
        r"January|February|March|April|May|June|July|August|September|October|November|December|"
        r"Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"
        r")(\s?\d{4})?\b",
        re.IGNORECASE
    )
    # 1–2 digit tokens, optionally inside [ ]
    day_token_pattern = re.compile(r"(?:\[\s*)?\b\d{1,2}\b(?:\s*\])?")

    # Indices of entries that are pure month headers
    month_header_indices = [i for i, e in enumerate(entries) if month_pattern.fullmatch(e["text"])]

    month_offset_y = 0
    start_pair_idx = None  # index into month_header_indices (the first month of the validated pair)

    if len(month_header_indices) >= 2:
        # Find the first consecutive pair that has >= 5 day tokens between them
        for i in range(len(month_header_indices) - 1):
            s = month_header_indices[i]
            t = month_header_indices[i + 1]
            between = entries[s:t]  # entries in the first month block
            day_like_count = sum(len(day_token_pattern.findall(e["text"])) for e in between)
            if day_like_count >= 5:
                start_pair_idx = i
                # Compute block height from the validated block
                if between:
                    min_y = min(e["y"] for e in between)
                    max_y = max(e["y"] + e["h"] for e in between)
                else:
                    # Edge: no lines between headers; use headers' boxes
                    min_y = entries[s]["y"]
                    max_y = entries[t]["y"] + entries[t]["h"]
                month_offset_y = (max_y - min_y) + 40
                break

    if start_pair_idx is not None and month_offset_y > 0:
        # Build block boundaries from the validated first month onward:
        # [start_of_block_0 (=first month), start_of_block_1 (=second month), ..., end (=len(entries))]
        boundaries = month_header_indices[start_pair_idx:] + [len(entries)]

        # Shift each block after the first by N * month_offset_y
        # block 0 gets 0 * offset, block 1 gets 1 * offset, etc.
        for block_idx in range(1, len(boundaries) - 1):
            block_start = boundaries[block_idx]
            block_end   = boundaries[block_idx + 1]
            shift = month_offset_y * block_idx
            for j in range(block_start, block_end):
                entries[j]["y"] += shift
            
    # Sort by (y, x, _orig_index) so items with identical y/x come in file order
    entries.sort(key=lambda e: (e["y"], e["x"], e["_orig_index"]))

    # Group into rows based on the row's min y
    # (If an item is more than y_threshold above the row's min y, start a new row)
    rows: list[list[dict]] = []
    X_NEAR = 320          # ← tune if needed; smaller ⇒ stricter “same line”
    EPS    = 1e-3         # ← float-noise tolerance for “exactly the same y”

    for entry in entries:
        # rows whose baseline-y is within y_threshold of this entry
        candidate_rows = [
            (idx, row) for idx, row in enumerate(rows)
            if abs(entry["y"] - min(e["y"] for e in row)) < y_threshold
        ]

        if not candidate_rows:                # -- first element of a new row
            rows.append([entry])
            continue

        # ➊ if any candidate row has essentially the *same y*, use it directly
        for idx, row in candidate_rows:
            if abs(entry["y"] - min(e["y"] for e in row)) < EPS:
                rows[idx].append(entry)
                break
        else:
            # ➋ otherwise pick the row whose closest x-distance is minimal
            best_idx, best_dist = None, float("inf")
            for idx, row in candidate_rows:
                dist = min(abs(entry["x"] - e["x"]) for e in row)
                if dist < best_dist:
                    best_dist = dist
                    best_idx = idx

            if best_dist < X_NEAR:
                rows[best_idx].append(entry)
            else:
                rows.append([entry])

    # Sort each row by x & _orig_index again, then store row info
    row_data = []
    for row in rows:
        row.sort(key=lambda e: (e["x"], e["_orig_index"]))
        row_min_y = min(e["y"] for e in row)
        row_data.append({"entries": row, "row_min_y": row_min_y})

    output_lines = []
    prev_row_min_y = None
    prev_row_height = 0  # Track how many lines the previous row occupied

    for row in row_data:
        row_min_y = row["row_min_y"]
                    
        # Calculate vertical gap from previous content (not just previous row min y)
        if prev_row_min_y is not None:
            # The actual bottom of previous content is prev_row_min_y + (prev_row_height * y_threshold)
            content_bottom = prev_row_min_y + (prev_row_height * y_threshold)
            gap = row_min_y - content_bottom
            
            # Calculate how many blank lines to add based on multiples of y_threshold
            if gap >= y_threshold:
                multiple = gap / y_threshold
                n_blank = int(multiple)  # Add one blank line per multiple of y_threshold
                for _ in range(n_blank):
                    output_lines.append("")                    

        # Track each entry's wrapped lines
        entry_lines = []
        max_wrapped_lines = 1
        
        for e in row["entries"]:
            max_col_width = max(1, int(round(e["w"] / h_scale_factor)))
            target_col = max(0, int(round(e["x"] / h_scale_factor)))
            
            wrapping_factor = 1.0
            if len(e["text"]) <= 40:
                wrapping_factor = 1.8
            if len(e["text"]) <= 6: # case like [1], [AI], {{2}}, {{AI}}
                wrapping_factor = 6
                
            if "||" in e["text"] or len(e["text"]) > max_col_width * wrapping_factor:
                wrapped = word_wrap(e["text"], max_col_width)
            else:
                wrapped = [e["text"]]
            
            entry_lines.append({
                "lines": wrapped,
                "target_col": target_col
            })
            max_wrapped_lines = max(max_wrapped_lines, len(wrapped))

        # Build row lines
        row_lines = [""] * max_wrapped_lines
        for entry in entry_lines:
            for i, line in enumerate(entry["lines"]):
                if i < len(row_lines):
                    row_lines[i] = place_text(row_lines[i], line, entry["target_col"])
                else:
                    row_lines.append(place_text("", line, entry["target_col"]))
        
        output_lines.extend(row_lines)
        prev_row_min_y = row_min_y
        prev_row_height = max_wrapped_lines  # Update for next iteration

    output = "\n".join(output_lines)
    output = _remove_excessive_whitespace(output)
    if additional_text:
        output = additional_text + "\n" + output
    return output

def _remove_excessive_whitespace(text: str) -> str:
    """
    Remove excessive internal whitespace from the text.
    Remove more than 3 consecutive empty lines to reduce to 3.
    """
    return re.sub(r'((?:\r?\n){4,})', '\n\n\n', text)

def place_text(existing_line: str, text: str, target: int) -> str:
    """
    Place 'text' into 'existing_line' at column 'target'.
    If existing_line is shorter, we pad with spaces.
    If existing_line is already long, we add a space if needed,
    then append the text.
    """
    if len(existing_line) < target:
        existing_line += " " * (target - len(existing_line))
    else:
        if existing_line and not existing_line.endswith(" "):
            existing_line += " "
    return existing_line + text

def word_wrap(text: str, max_width: int) -> list:
    """
    Splits 'text' into multiple lines, never exceeding 'max_width' characters.
    Wraps at word boundaries. If '||' is found, force line breaks at those points.
    """
    if "||" in text:
        segments = [s.strip() for s in text.split("||")]   # keep empties for check
        wrapped_lines = []
        for seg in segments:
            seg = seg.strip()
            if seg == "-" or not seg:          # ① skip the lone dash (or blanks)
                continue
            wrapped_lines.extend(word_wrap(seg, max_width))   # recurse / normal wrap
        return wrapped_lines

    # Regular word wrapping
    words = text.split()
    lines = []
    current_line = ""

    for w in words:
        # if adding w + space to current_line would exceed max_width
        if current_line:
            if len(current_line) + 1 + len(w) <= max_width:
                current_line += " " + w
            else:
                lines.append(current_line)
                current_line = w
        else:
            # if w is bigger than max_width, forcibly break
            if len(w) > max_width:
                forced_lines = [
                    w[i : i + max_width] for i in range(0, len(w), max_width)
                ]
                if forced_lines:
                    current_line = forced_lines[0]
                    for chunk in forced_lines[1:]:
                        lines.append(current_line)
                        current_line = chunk
            else:
                current_line = w

    if current_line:
        lines.append(current_line)

    return lines
