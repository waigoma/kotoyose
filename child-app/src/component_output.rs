use std::path::Path;

fn slash(byte: u8) -> bool {
    matches!(byte, b'/' | b'\\')
}

fn absolute(tail: &[u8]) -> bool {
    tail.starts_with(b"/")
        || tail.starts_with(b"\\\\")
        || tail.starts_with(b"file:/")
        || matches!(tail, [drive, b':', separator, ..] if drive.is_ascii_alphabetic() && slash(*separator))
}

fn runtime_path(tail: &str, root: &str) -> bool {
    if root.is_empty() {
        return false;
    }
    let tail = tail.strip_prefix("file:///").unwrap_or(tail);
    let tail = tail.strip_prefix(r"\\?\").unwrap_or(tail);
    let tail = if root.starts_with('/') && !tail.starts_with('/') && !tail.contains(':') {
        // A file URI with a POSIX path uses the third slash as its root.
        return runtime_path(&format!("/{tail}"), root);
    } else {
        tail
    };
    let windows = root.as_bytes().get(1) == Some(&b':') || root.starts_with(r"\\");
    tail.get(..root.len()).is_some_and(|prefix| {
        if windows {
            prefix
                .bytes()
                .zip(root.bytes())
                .all(|(a, b)| a.eq_ignore_ascii_case(&b) || (slash(a) && slash(b)))
        } else {
            prefix == root
        }
    }) && tail.as_bytes().get(root.len()).is_none_or(|&byte| {
        slash(byte)
            || byte.is_ascii_whitespace()
            || matches!(byte, b'"' | b'\'' | b')' | b']' | b',')
    })
}

pub fn redact(line: &str, runtime_root: Option<&Path>) -> String {
    let root = runtime_root.map(|path| path.to_string_lossy());
    let mut output = String::with_capacity(line.len());
    let mut remaining = line;
    let mut previous = ' ';
    while let Some(ch) = remaining.chars().next() {
        let boundary = previous.is_whitespace()
            || matches!(
                previous,
                '\'' | '"' | '(' | '[' | '{' | '=' | '<' | '>' | ':' | ','
            );
        let url = previous == ':' && remaining.starts_with("//");
        if url {
            let end = remaining
                .find(|ch: char| ch.is_whitespace() || matches!(ch, '"' | '<' | '>'))
                .unwrap_or(remaining.len());
            let token = &remaining[2..end];
            let address = token.trim_end_matches(['\'', ')', ',']);
            let suffix = &token[address.len()..];
            let address = address.split(['?', '#']).next().unwrap_or_default();
            let authority_end = address.find('/').unwrap_or(address.len());
            let authority = &address[..authority_end];
            output.push_str("//");
            output.push_str(authority.rsplit('@').next().unwrap_or_default());
            output.push_str(&address[authority_end..]);
            output.push_str(suffix);
            remaining = &remaining[end..];
            previous = 'x';
        } else if boundary && absolute(remaining.as_bytes()) {
            let quote = matches!(previous, '\'' | '"').then_some(previous);
            // Pip may print unquoted paths with spaces. Consume through the surrounding
            // punctuation or the end of that diagnostic rather than exposing a suffix.
            let end = remaining
                .find(|character| match quote {
                    Some(quote) => character == quote,
                    None => matches!(character, ')' | ']' | '}' | '>' | '"' | '\'' | '|'),
                })
                .unwrap_or(remaining.len());
            let (path, rest) = remaining.split_at(end);
            let value = if root.as_ref().is_some_and(|root| runtime_path(path, root)) {
                "<runtime>"
            } else {
                "<path>"
            };
            output.push_str(value);
            remaining = rest;
            previous = 'x';
        } else {
            output.push(ch);
            remaining = &remaining[ch.len_utf8()..];
            previous = ch;
        }
    }
    output
}

#[cfg(test)]
mod tests {
    use std::path::Path;

    use super::redact;

    #[test]
    fn runtime_paths_are_replaced_and_other_absolute_paths_are_removed() {
        let root = Path::new(r"C:\Users\ada\AppData\Local\Programs\Kotoyose Child\1.2.0\python");
        let line = format!(
            "Could not replace '{}\\locked file' (see D:\\secrets\\token.txt) via https://pypi.org/simple/pkg",
            root.display()
        );
        assert_eq!(
            redact(&line, Some(root)),
            "Could not replace '<runtime>' (see <path>) via https://pypi.org/simple/pkg"
        );
    }

    #[test]
    fn posix_runtime_and_urls_keep_their_roles() {
        let root = Path::new("/opt/kotoyose/python");
        let line = "error at (/opt/kotoyose/python/lib/site-packages/pkg) and (/etc/passwd) via https://example.test/a";
        assert_eq!(
            redact(line, Some(root)),
            "error at (<runtime>) and (<path>) via https://example.test/a"
        );
    }
}
