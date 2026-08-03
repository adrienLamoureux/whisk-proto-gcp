import type { Media } from "../lib/api";

interface Props {
  items: Media[];
  emptyLabel: string;
  onFavorite?: (media: Media) => void;
  onShare?: (media: Media) => void;
  onDelete?: (media: Media) => void;
}

export function MediaGrid({ items, emptyLabel, onFavorite, onShare, onDelete }: Props) {
  if (items.length === 0) return <div className="wsk-empty">{emptyLabel}</div>;

  return (
    <div className="wsk-media-grid">
      {items.map((media) => (
        <figure className="wsk-media" key={media.mediaId} style={{ margin: 0 }}>
          {/* Media URLs are short-lived signed URLs; a stale one 403s rather
              than leaking, so a broken image here is expected after ~15min. */}
          <img src={media.url} alt={media.prompt || "generated image"} loading="lazy" />
          {media.prompt && <figcaption className="wsk-media-caption">{media.prompt}</figcaption>}
          {(onFavorite || onShare || onDelete) && (
            <div className="wsk-media-bar">
              {onFavorite && (
                <button onClick={() => onFavorite(media)} title="Favourite">
                  {media.favorite ? "★" : "☆"}
                </button>
              )}
              {onShare && (
                <button onClick={() => onShare(media)} disabled={media.shared}>
                  {media.shared ? "shared" : "share"}
                </button>
              )}
              {onDelete && <button onClick={() => onDelete(media)}>delete</button>}
            </div>
          )}
        </figure>
      ))}
    </div>
  );
}
